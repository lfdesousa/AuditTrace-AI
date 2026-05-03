#!/usr/bin/env bash
# Post-deploy verification gate — Phase C.12.
#
# Run AFTER `make k8s-rolling-image` (or any helm upgrade) to assert
# the cluster is in a known-good state. Designed for the M5 off-LAN
# rehearsal (2026-05-15) where we need a one-shot green/red answer
# rather than eyeballing kubectl + Tempo + Loki manually.
#
# Each check prints PASS / FAIL / SKIP with a one-line reason. Final
# exit code is 0 only if ZERO checks failed (SKIPs do not fail the
# gate — they downgrade confidence but don't block).
#
# Exit codes:
#   0   — all checks passed (some may have skipped)
#   1   — environment problem (no kubectl, no helm, can't reach cluster)
#   2   — at least one check FAILED — cluster is NOT in expected state

set -euo pipefail

NAMESPACE="${NAMESPACE:-audittrace}"
RELEASE="${RELEASE:-audittrace}"
TEMPO_URL="${TEMPO_URL:-http://192.168.1.231:3200}"
LOKI_URL="${LOKI_URL:-http://192.168.1.231:3100}"
# 50 is generous for a post-deploy window: a healthy cluster typically
# emits a handful of ERROR lines from boot-time Istio sidecar races and
# a chart upgrade can briefly multiply that. A real disaster lands in
# the hundreds. Operators wanting tighter monitoring set the env var.
LOKI_ERROR_THRESHOLD="${LOKI_ERROR_THRESHOLD:-50}"
KUBECONFIG_FLAG=""
if [ -n "${KUBECONFIG:-}" ]; then
    KUBECONFIG_FLAG="--kubeconfig=$KUBECONFIG"
elif [ -f "$HOME/.kube/config" ]; then
    KUBECONFIG_FLAG="--kubeconfig=$HOME/.kube/config"
fi

PASS=0
FAIL=0
SKIP=0

pass()   { echo "[verify]  ✓ $1"; PASS=$((PASS+1)); }
fail()   { echo "[verify]  ✗ $1" >&2; FAIL=$((FAIL+1)); }
skip()   { echo "[verify]  · $1 (SKIP)"; SKIP=$((SKIP+1)); }
header() { echo "[verify]"; echo "[verify] $1"; }

# ── 0. environment ──────────────────────────────────────────────────────────
for tool in kubectl helm; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "[verify] ERROR: $tool not on PATH (exit 1)" >&2
        exit 1
    fi
done

if ! kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get pods -o name >/dev/null 2>&1; then
    echo "[verify] ERROR: cannot reach cluster / namespace $NAMESPACE (exit 1)" >&2
    exit 1
fi

echo "[verify] === audittrace post-deploy verification ==="
echo "[verify] namespace=$NAMESPACE release=$RELEASE"

# ── 1. All chart pods Ready ──────────────────────────────────────────────────
header "(1/8) Pod readiness"
not_ready=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get pods --no-headers 2>/dev/null \
    | awk '{
        split($2, ready, "/")
        if (ready[1] != ready[2] && $3 != "Completed") print $0
      }')
if [ -z "$not_ready" ]; then
    pass "all pods Ready (or Completed)"
else
    fail "pods not Ready:"
    echo "$not_ready" | sed 's/^/[verify]      /' >&2
fi

# ── 2. No CrashLoopBackOff or Error pods ────────────────────────────────────
header "(2/8) No crashing pods"
crashing=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get pods --no-headers 2>/dev/null \
    | awk '$3 == "CrashLoopBackOff" || $3 == "Error" || $3 == "ErrImagePull" {print}')
if [ -z "$crashing" ]; then
    pass "no CrashLoopBackOff / Error / ErrImagePull"
else
    fail "crashing pods:"
    echo "$crashing" | sed 's/^/[verify]      /' >&2
fi

# ── 3. Helm release status `deployed` ───────────────────────────────────────
header "(3/8) Helm release status"
release_status=$(helm $KUBECONFIG_FLAG status "$RELEASE" -n "$NAMESPACE" \
    -o json 2>/dev/null | jq -r '.info.status // "unknown"')
if [ "$release_status" = "deployed" ]; then
    pass "release '$RELEASE' status=deployed"
else
    fail "release '$RELEASE' status=$release_status (expected: deployed)"
fi

# ── 4. Memory-server /health returns 200 ────────────────────────────────────
header "(4/8) Memory-server /health"
ms_pod=$(kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" get pod \
    -l app.kubernetes.io/component=memory-server \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [ -z "$ms_pod" ]; then
    fail "no memory-server pod found"
elif kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" exec "$ms_pod" -c memory-server \
        -- curl -s -o /dev/null -w "%{http_code}" http://localhost:8765/health 2>/dev/null \
        | grep -q "^200$"; then
    pass "memory-server /health returned 200"
else
    fail "memory-server /health did not return 200"
fi

# ── 5. Memory-server /metrics reachable ─────────────────────────────────────
header "(5/8) Memory-server /metrics"
if [ -n "$ms_pod" ] && kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" exec "$ms_pod" \
        -c memory-server \
        -- curl -s -o /dev/null -w "%{http_code}" http://localhost:8765/metrics 2>/dev/null \
        | grep -qE "^(200|401)$"; then
    # 401 is acceptable: /metrics is auth-gated; the endpoint IS reachable.
    pass "memory-server /metrics endpoint reachable"
else
    fail "memory-server /metrics not reachable"
fi

# ── 6. Postgres reachable (pg_isready from inside the pg pod) ───────────────
header "(6/8) Postgres reachability"
if kubectl $KUBECONFIG_FLAG -n "$NAMESPACE" exec audittrace-postgresql-0 \
        -c postgresql -- pg_isready -U postgres -d audittrace 2>&1 \
        | grep -q "accepting connections"; then
    pass "postgres accepting connections"
else
    fail "postgres pg_isready failed"
fi

# ── 7. Recent Tempo trace activity for audittrace-server ────────────────────
header "(7/8) Tempo: recent traces for audittrace-server"
# 30-min window; if nothing is using the system, this can legitimately be
# empty — flag that as SKIP rather than FAIL so a quiet cluster passes.
if ! curl --silent --connect-timeout 3 --max-time 10 \
        "${TEMPO_URL}/api/echo" >/dev/null 2>&1; then
    skip "Tempo unreachable at ${TEMPO_URL}"
else
    end=$(date +%s)
    start=$((end - 1800))
    found=$(curl --silent --max-time 15 \
        "${TEMPO_URL}/api/search?tags=service.name%3Daudittrace-server&start=${start}&end=${end}&limit=1" \
        2>/dev/null | jq -r '.traces | length // 0')
    if [ "$found" = "0" ] || [ -z "$found" ]; then
        skip "no traces in last 30 min (cluster may be idle)"
    else
        pass "found $found+ recent audittrace-server traces"
    fi
fi

# ── 8. Loki: ERROR-level audittrace lines below threshold ───────────────────
header "(8/8) Loki: audittrace ERROR rate"
if ! curl --silent --connect-timeout 3 --max-time 10 \
        "${LOKI_URL}/ready" >/dev/null 2>&1; then
    skip "Loki unreachable at ${LOKI_URL}"
else
    end_ns=$(date +%s)000000000
    start_ns=$(($(date +%s) - 1800))000000000
    # Count audittrace-namespaced ERROR lines (LogQL `count_over_time`).
    err_count=$(curl --silent --max-time 15 -G "${LOKI_URL}/loki/api/v1/query" \
        --data-urlencode 'query=count_over_time({namespace="audittrace"} |= "ERROR" [30m])' \
        --data-urlencode "time=${end_ns}" 2>/dev/null \
        | jq -r '[.data.result[].value[1] // "0"] | map(tonumber) | add // 0')
    err_count=${err_count:-0}
    if [ "$err_count" -le "$LOKI_ERROR_THRESHOLD" ]; then
        pass "Loki ERROR count over 30m = $err_count (threshold $LOKI_ERROR_THRESHOLD)"
    else
        fail "Loki ERROR count over 30m = $err_count (threshold $LOKI_ERROR_THRESHOLD exceeded)"
    fi
fi

# ── Summary ─────────────────────────────────────────────────────────────────
echo "[verify]"
echo "[verify] ─────────────────────────────────────────"
echo "[verify]  Summary:  $PASS passed | $FAIL failed | $SKIP skipped"
echo "[verify] ─────────────────────────────────────────"

if [ "$FAIL" -gt 0 ]; then
    echo "[verify] gate FAILED — cluster is not in expected state" >&2
    exit 2
fi
echo "[verify] gate PASSED"
exit 0
