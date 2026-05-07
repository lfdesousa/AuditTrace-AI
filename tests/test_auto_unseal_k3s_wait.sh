#!/usr/bin/env bash
# Bash-test for the k3s-readiness wait in
# scripts/audittrace-vault-auto-unseal.sh.
#
# Three scenarios — each runs the real script with a stubbed kubectl
# (PATH shim) and short timeouts (K3S_WAIT_MAX_S=10, K3S_WAIT_POLL_S=1)
# so the suite finishes in seconds:
#
#   1. k3s_ready_immediately   — kubectl /livez succeeds on call #1.
#                                Script must proceed past the wait,
#                                hit the (stubbed) "already unsealed"
#                                idempotent path, exit 0.
#   2. k3s_ready_after_3_polls — kubectl /livez fails calls 1-3,
#                                succeeds on call 4. Script must wait
#                                ~3s, then proceed and exit 0.
#   3. k3s_never_ready         — kubectl /livez always fails. Script
#                                must exhaust K3S_WAIT_MAX_S=10s and
#                                exit 10 (the reserved code that lets
#                                systemd's Restart=on-failure cycle
#                                the unit per ADR-049 §recovery).
#
# Why this matters: the production failure on 2026-05-07 was a
# Requires=k3s.service unit getting cancelled when k3s's first start
# attempt failed. The unit was changed to Wants= and this readiness
# wait was added to the script so the recovery path runs inside the
# unit's own retry loop. Without these tests we re-introduce the
# 9h sealed-vault outage the next time the network-online race fires.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/audittrace-vault-auto-unseal.sh"

if [[ ! -x "$SCRIPT" ]]; then
  echo "ERROR: $SCRIPT not executable" >&2
  exit 1
fi

PASS=0
FAIL=0
RESULTS=()

# ── helpers ──────────────────────────────────────────────────────────

# make_stub_kubectl <stub_dir> <livez_succeed_after> <vault_already_unsealed>
#   Writes a fake kubectl into <stub_dir> that:
#     - For `kubectl ... get --raw=/livez`:
#         maintains a call-counter in <stub_dir>/livez_calls, returns
#         exit 0 once the counter is >= <livez_succeed_after> (1-based;
#         so 1 means "succeed on first call").
#     - For `kubectl ... exec ... vault status`:
#         emits `Initialized true / Sealed false` if
#         <vault_already_unsealed> is "true", otherwise nothing.
#     - Anything else: exit 0 (we don't reach those paths in tests).
#
#   livez_succeed_after=99 effectively means "never succeed" in
#   our 10-call test windows.
make_stub_kubectl() {
  local stub_dir="$1"
  local livez_succeed_after="$2"
  local vault_already_unsealed="$3"

  mkdir -p "$stub_dir"
  cat > "$stub_dir/kubectl" <<STUB
#!/usr/bin/env bash
# Stub kubectl for the auto-unseal bash test runner.
set -u
COUNTER_FILE="$stub_dir/livez_calls"

# Detect /livez probe.
for arg in "\$@"; do
  if [[ "\$arg" == "--raw=/livez" ]]; then
    n=\$(cat "\$COUNTER_FILE" 2>/dev/null || echo 0)
    n=\$((n + 1))
    echo "\$n" > "\$COUNTER_FILE"
    if (( n >= $livez_succeed_after )); then
      exit 0
    else
      exit 1
    fi
  fi
done

# Detect 'exec ... vault status'.
if [[ "\$*" == *"exec"* && "\$*" == *"vault status"* ]]; then
  if [[ "$vault_already_unsealed" == "true" ]]; then
    cat <<'EOF'
Key             Value
---             -----
Initialized     true
Sealed          false
Total Shares    5
Threshold       3
EOF
    exit 0
  fi
  exit 0
fi

# Default: succeed silently.
exit 0
STUB
  chmod +x "$stub_dir/kubectl"
}

# run_case <name> <livez_succeed_after> <expected_exit_code> <max_wallclock_s>
run_case() {
  local name="$1"
  local livez_succeed_after="$2"
  local expected_exit="$3"
  local max_wallclock="$4"

  local stub_dir
  stub_dir=$(mktemp -d -t k3sstub.XXXXXX)
  trap 'rm -rf "$stub_dir"' RETURN

  make_stub_kubectl "$stub_dir" "$livez_succeed_after" "true"

  # Minimal valid init file: 3 base64 keys (only required if the
  # script reaches the unseal path; in our scenarios it doesn't,
  # because the stubbed `vault status` reports already-unsealed
  # and the idempotent check exits 0 first — but the script still
  # validates the file is present + readable up front).
  local init_file="$stub_dir/init.json"
  cat > "$init_file" <<'EOF'
{
  "unseal_keys_b64": [
    "dGVzdC1rZXktMS1zdHViLW5vdC1hLXJlYWwta2V5MQ==",
    "dGVzdC1rZXktMi1zdHViLW5vdC1hLXJlYWwta2V5Mg==",
    "dGVzdC1rZXktMy1zdHViLW5vdC1hLXJlYWwta2V5Mw==",
    "dGVzdC1rZXktNC1zdHViLW5vdC1hLXJlYWwta2V5NA==",
    "dGVzdC1rZXktNS1zdHViLW5vdC1hLXJlYWwta2V5NQ=="
  ],
  "root_token": "stub-root-token-not-real"
}
EOF
  chmod 600 "$init_file"

  local logfile="$stub_dir/script.log"
  local start_ts
  start_ts=$(date +%s)

  set +e
  PATH="$stub_dir:$PATH" \
    K3S_WAIT_MAX_S=10 K3S_WAIT_POLL_S=1 \
    KUBECONFIG=/dev/null \
    VAULT_INIT_FILE="$init_file" \
    bash "$SCRIPT" >"$logfile" 2>&1
  local got_exit=$?
  set -e

  local end_ts
  end_ts=$(date +%s)
  local elapsed=$((end_ts - start_ts))

  local ok=true
  if [[ "$got_exit" != "$expected_exit" ]]; then
    ok=false
  fi
  if (( elapsed > max_wallclock )); then
    ok=false
  fi

  if $ok; then
    printf "[PASS] %-30s  exit=%d (expected %d), elapsed=%ds (max %ds)\n" \
      "$name" "$got_exit" "$expected_exit" "$elapsed" "$max_wallclock"
    PASS=$((PASS + 1))
    RESULTS+=("PASS|$name|exit=$got_exit|elapsed=${elapsed}s")
  else
    printf "[FAIL] %-30s  exit=%d (expected %d), elapsed=%ds (max %ds)\n" \
      "$name" "$got_exit" "$expected_exit" "$elapsed" "$max_wallclock"
    echo "      script log:" >&2
    sed 's/^/        /' "$logfile" >&2
    FAIL=$((FAIL + 1))
    RESULTS+=("FAIL|$name|exit=$got_exit|elapsed=${elapsed}s")
  fi

  rm -rf "$stub_dir"
}

# ── scenarios ────────────────────────────────────────────────────────

# Scenario 1: k3s ready immediately. The script should hit the
# readiness probe, succeed on call 1, then proceed to vault_status
# which returns "already unsealed" → idempotent exit 0. Wallclock
# should be very small (< 3s) because no sleeping in the wait loop.
run_case "k3s_ready_immediately" 1 0 5

# Scenario 2: k3s ready after 3 polls. With K3S_WAIT_POLL_S=1, the
# script sleeps 1s between calls, so total elapsed >= 3s but < 10s.
# Expected exit 0 (idempotent vault path).
run_case "k3s_ready_after_3_polls" 4 0 8

# Scenario 3: k3s never ready. The script should exhaust the 10s
# window and exit 10 — the reserved code that lets systemd's
# Restart=on-failure retry the whole unit.
run_case "k3s_never_ready" 99 10 14

# ── summary ──────────────────────────────────────────────────────────

echo
echo "Summary: $PASS passed, $FAIL failed"
if (( FAIL > 0 )); then
  exit 1
fi
exit 0
