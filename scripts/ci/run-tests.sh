#!/usr/bin/env bash
# scripts/ci/run-tests.sh — THE canonical test-suite invocation.
#
# SPEC 2026-10-11-SPEC-test-parity-scripted-enforcement.md (R1/R4), carried
# through ADDENDUM A1-A7 after the suite's own hermeticity leaked a REAL
# kubectl call on the laptop (PR #380, CI run 37878027812) and after two
# independent-review REJECTs on the first two rebuilds (each round parsed
# more argv/wrapper forms; the reviewer kept finding new ones). This round
# changes the INSTRUMENT: THIS SCRIPT is the process-layer CONTROL —
# tests/conftest.py's autouse guard is a diagnostic, not the control.
#
# Usage:
#   scripts/ci/run-tests.sh                  # the full gate: --cov, --cov-fail-under=90, junit.xml
#   scripts/ci/run-tests.sh --subset "<expr>" [--junit <path>]
#                                             # `make test-unit`'s fast subset mode —
#                                             # same CI shape, own junit, own zero-skip
#                                             # check, but no --cov enforcement (a partial
#                                             # run can't meaningfully satisfy the gate).
#
# `make test` (inside the local-only `hold-shared` wrapper) and the CI
# `test` job's "Tests + ..." step both call exactly `bash scripts/ci/run-
# tests.sh` — tests/test_ci_make_test_parity.py asserts that mechanically
# against the shipped ci.yml and Makefile.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# ---- fail closed on a stray global pytest-arg override (A2-9) ----
if [ -n "${PYTEST_ADDOPTS:-}" ]; then
  echo "❌ run-tests.sh: PYTEST_ADDOPTS is set (${PYTEST_ADDOPTS@Q}) — this" >&2
  echo "   script owns the pytest invocation; unset PYTEST_ADDOPTS and retry." >&2
  exit 1
fi

# ---- argument parsing (subset mode) ----
SUBSET_EXPR=""
JUNIT_PATH="junit.xml"
while [ $# -gt 0 ]; do
  case "$1" in
    --subset)
      SUBSET_EXPR="$2"
      JUNIT_PATH="junit-subset.xml"
      shift 2
      ;;
    --junit)
      JUNIT_PATH="$2"
      shift 2
      ;;
    *)
      echo "❌ run-tests.sh: unknown argument $1" >&2
      exit 1
      ;;
  esac
done

# ---- tool resolution (CI-PARITY rule: .venv/bin/X if present, else PATH,
# else fail closed — CI has no .venv at all). ----
resolve_tool() {
  local name="$1"
  if [ -x ".venv/bin/$name" ]; then
    echo ".venv/bin/$name"
  elif command -v "$name" >/dev/null 2>&1; then
    command -v "$name"
  else
    echo "❌ $name not found (.venv/bin/$name or PATH) — install dev deps first (make venv)" >&2
    exit 1
  fi
}

PYTEST="$(resolve_tool pytest)"
PYTHON="$(resolve_tool python)"

# ---- G5 (DEP-LOCK-AT C2, WU-2): every environment this run depends on MUST be
# its hashed lock, exactly — fail closed, no flag, no env bypass. A venv that
# drifted from requirements-dev.txt (the 2026-10-18 otel 1.44.0 / fastapi
# 0.140.7 venvs) is refused BEFORE a single test runs, and the same check runs
# again AFTER pytest (a test that installs or upgrades a package is caught, not
# just a stale venv: residual 4). Recovery: `rm -rf .venv && make venv`.
#
# BL-1: the suite runs under $PYTEST's interpreter, which is not necessarily
# $PYTHON (a console script's shebang decides). G5 checks BOTH.
# The gate venv (semgrep, requirements-gate.txt) is the second lock
# environment; the three images are checked by check-images-match-locks.sh.
GATE_VENV="${GATE_VENV:-.gate-venv}"
DEV_LOCK=requirements-dev.txt
GATE_LOCK=requirements-gate.txt

resolve_pytest_python() {
  # The interpreter the pytest console script runs under: its shebang when that
  # names a python, else a sibling `python` in the same directory; else fail
  # closed (an interpreter we cannot identify is an interpreter we cannot check).
  local first interp="" word2
  first="$(head -n 1 "$PYTEST" 2>/dev/null || true)"
  case "$first" in
    '#!'*)
      read -r interp word2 <<< "${first#'#!'}"
      if [ "$(basename "$interp")" = "env" ]; then
        interp="$(command -v "$word2" 2>/dev/null || true)"
      fi
      ;;
  esac
  if [ -n "$interp" ] && [ -x "$interp" ] && [[ "$(basename "$interp")" == python* ]]; then
    echo "$interp"
  elif [ -x "$(dirname "$PYTEST")/python" ]; then
    echo "$(dirname "$PYTEST")/python"
  else
    echo "❌ run-tests.sh: cannot identify the interpreter $PYTEST runs under (G5/BL-1)" >&2
    return 1
  fi
}

PYTEST_PYTHON="$(resolve_pytest_python)"

check_environments() { # check_environments <when: pre|post>
  local when="$1"
  echo "run-tests: G5 ($when-pytest): $PYTHON / $PYTEST_PYTHON == $DEV_LOCK, $GATE_VENV == $GATE_LOCK"
  "$PYTHON" scripts/ci/check_venv_matches_lock.py "$DEV_LOCK"
  if [ "$PYTEST_PYTHON" != "$PYTHON" ]; then
    "$PYTEST_PYTHON" scripts/ci/check_venv_matches_lock.py "$DEV_LOCK"
  fi
  if [ ! -x "$GATE_VENV/bin/python" ]; then
    echo "❌ run-tests.sh: gate venv $GATE_VENV/bin/python not found — run: make gate-venv" >&2
    exit 1
  fi
  "$PYTHON" scripts/ci/check_freeze_matches_lock.py \
    --lock "$GATE_LOCK" --python "$GATE_VENV/bin/python" --label "gate venv"
}

check_environments pre

# ---- R4/A3-8: resolve the REAL helm BEFORE the PATH change — the shim
# execs this absolute path on an allowed call. Fail at start if missing;
# both the CI runner and the laptop have helm. ----
if ! REAL_HELM="$(command -v helm)"; then
  echo "❌ run-tests.sh: helm not found on PATH — required to resolve the" >&2
  echo "   real binary the shim execs on an allowed call." >&2
  exit 1
fi

# ---- the process-layer shim dir: FIRST on PATH, ships both kubectl and
# helm. A non-empty reject log (either tool) fails this script below. ----
SHIM_DIR="$(mktemp -d "${TMPDIR:-/tmp}/audittrace-run-tests-shim.XXXXXX")"
REJECT_LOG="$SHIM_DIR/reject.log"
: > "$REJECT_LOG"

cat > "$SHIM_DIR/kubectl" <<SHIM_EOF
#!/usr/bin/env bash
# Sentinel kubectl (scripts/ci/run-tests.sh). A test that reaches this
# leaked past tests/conftest.py's in-process diagnostic — it is touching a
# real cluster tool from inside the test suite. Log the call and ALWAYS
# fail, exactly like a CI runner that has no cluster at all.
echo "\$(date -Iseconds) kubectl \$*" >> "$REJECT_LOG"
exit 17
SHIM_EOF
chmod +x "$SHIM_DIR/kubectl"

cat > "$SHIM_DIR/helm" <<SHIM_EOF
#!/usr/bin/env bash
# Sentinel helm (scripts/ci/run-tests.sh, A2-1/A3-8). Runs the SAME
# positional ALLOW rule as tests/conftest.py's Python rule (both driven
# off tests/data/helm_rule_cases.json on 'list' rows — see
# tests/test_run_tests_sh_enforcement.py): template/lint/dependency
# list/version/show, with no --validate/--dry-run/--kube* argument, pass
# straight through to the REAL helm resolved at script start. Anything
# else is logged and rejected, exactly like a CI runner with no cluster.
set -euo pipefail
safe=0
case "\${1:-}" in
  template|lint|version|show)
    safe=1
    ;;
  dependency)
    if [ "\${2:-}" = "list" ]; then
      safe=1
    fi
    ;;
esac
if [ "\$safe" -eq 1 ]; then
  for arg in "\$@"; do
    case "\$arg" in
      --validate*|--dry-run*|--kube*)
        safe=0
        ;;
    esac
  done
fi
if [ "\$safe" -eq 1 ]; then
  exec "$REAL_HELM" "\$@"
fi
echo "\$(date -Iseconds) helm \$*" >> "$REJECT_LOG"
exit 17
SHIM_EOF
chmod +x "$SHIM_DIR/helm"

# ---- R4: force the CI shape — no reachable cluster, ever, from this
# script, whether it runs locally or in CI. ----
export KUBECONFIG="$SHIM_DIR/nonexistent-kubeconfig"
export PATH="$SHIM_DIR:$PATH"
# Prefix-enumerated, not a fixed name list: real in-cluster pods also get
# KUBERNETES_SERVICE_PORT_HTTPS (and potentially other KUBERNETES_SERVICE_*
# variants), and helm's own env vars are not limited to the 8 named ones
# `helm env` happens to print today. `compgen -e` lists every exported env
# var name; unset any matching either prefix.
for _prefixed_var in $(compgen -e | grep -E '^(KUBERNETES_SERVICE_|HELM_KUBE)'); do
  unset "$_prefixed_var" 2>/dev/null || true
done

# ---- the three sentinels tests/conftest.py's in-process diagnostic reads
# ONCE at import, so the SAME env-equality check applies to every spawn in
# this run (A5-1, A6-3/A4-2(j)). ----
export HERMETIC_SHIM_DIR="$SHIM_DIR"
export HERMETIC_KUBECONFIG="$KUBECONFIG"
export HERMETIC_REQUIRED=1

# ---- BL-4: signal handling. An unexplained SIGTERM (2026-10-21 13:57) orphaned
# a pytest and 4 ephemeral postgres containers: bash defers a trap while a
# foreground child runs, pytest's SIGTERM default skips atexit, and the
# containers are named after the pytest pid, which the wrapper never knew. So:
# (1) pytest runs as a background job in ITS OWN process group (`set -m`) and the
# wrapper `wait`s, so the trap runs at once; (2) the trap TERMs that whole group,
# KILLs it after a grace period, then (3) removes every container labelled with
# this run's id (tests/_pg_ephemeral.py labels them from RUN_TESTS_ID, deliberately
# NOT AUDITTRACE_-prefixed: tests/conftest.py wipes every AUDITTRACE_* variable at
# import, so a prefixed name never reached the labelling code in a real run).
RUN_ID="run-tests-$$-$(date +%s)"
export RUN_TESTS_ID="$RUN_ID"
CHILD_PID=""
TERM_GRACE_POLLS=50   # x 0.2 s = 10 s between TERM and KILL

reap_containers() {
  command -v docker >/dev/null 2>&1 || return 0
  local ids
  ids="$(docker ps -aq --filter "label=audittrace.run-tests-id=$RUN_ID" 2>/dev/null || true)"
  if [ -n "$ids" ]; then
    # shellcheck disable=SC2086
    docker rm -f $ids >/dev/null 2>&1 || true
  fi
}

terminate() { # terminate <signal> <exit code>
  trap '' TERM INT
  echo "run-tests: caught SIG$1 — stopping pytest (pid ${CHILD_PID:-none}) and its containers" >&2
  if [ -n "$CHILD_PID" ]; then
    kill -TERM -- "-$CHILD_PID" 2>/dev/null || kill -TERM "$CHILD_PID" 2>/dev/null || true
    # Grace period (10 s) polled, not a background watchdog: a leftover `sleep`
    # would hold the caller's stdout/stderr pipes open after this script exits.
    for _ in $(seq 1 "$TERM_GRACE_POLLS"); do
      kill -0 "$CHILD_PID" 2>/dev/null || break
      sleep 0.2
    done
    kill -KILL -- "-$CHILD_PID" 2>/dev/null || true
    wait "$CHILD_PID" 2>/dev/null || true
  fi
  reap_containers
  exit "$2"
}

run_pytest() { # run_pytest <pytest args...> — foreground semantics, signal-safe
  trap 'terminate TERM 143' TERM
  trap 'terminate INT 130' INT
  set -m   # job control stays on: the pin test forbids any `set +` line
  "$PYTEST" "$@" &
  CHILD_PID=$!
  local rc=0
  wait "$CHILD_PID" || rc=$?
  CHILD_PID=""
  reap_containers
  return "$rc"
}

cleanup() { reap_containers; rm -rf "$SHIM_DIR"; }
trap cleanup EXIT

echo "run-tests: forcing CI shape — KUBECONFIG=$KUBECONFIG (does not exist), kubectl+helm shim at $SHIM_DIR"
echo "run-tests: diagnostic probes (non-fatal, informational only):"
command -v helm >/dev/null 2>&1 && echo "run-tests: command -v helm -> $(command -v helm)" || echo "run-tests: command -v helm -> not found"
curl -sS --max-time 2 http://localhost:8080 >/dev/null 2>&1 || echo "probe localhost:8080 rc=$?"

if [ -n "$SUBSET_EXPR" ]; then
  # ---- subset mode (make test-unit): a fast, filtered run over the SAME
  # tests/ directory, in the SAME CI shape, with its OWN junit and its OWN
  # zero-skip check — but no --cov-fail-under enforcement (a filtered
  # subset can't meaningfully satisfy the per-file gate). ----
  echo "run-tests: subset mode -k \"$SUBSET_EXPR\" -> $JUNIT_PATH"
  run_pytest tests/ -v -k "$SUBSET_EXPR" --no-cov --junit-xml="$JUNIT_PATH"
  check_environments post

  echo "🚫 Enforcing zero-skip policy on the subset run..."
  "$PYTHON" scripts/check-no-skipped-tests.py "$JUNIT_PATH"
else
  # ---- R1: THE pytest invocation — the --cov set, the reports,
  # --cov-fail-under, --junit-xml. Nothing else in this repo may hand-type
  # this line. ----
  run_pytest tests/ -v \
    --cov=src \
    --cov=bff \
    --cov=scripts/deploy \
    --cov=scripts/hooks \
    --cov=scripts/release \
    --cov=scripts/migrate \
    --cov=scripts/curator \
    --cov=scripts/network \
    --cov=scripts/neuter \
    --cov=scripts/ci \
    --cov=scripts.replay_dead_lettered_index \
    --cov=scripts.backfill_pdf_signature_status \
    --cov=scripts.integration_gate \
    --cov-report=term-missing \
    --cov-report=xml \
    --cov-report=html:htmlcov \
    --cov-fail-under=90 \
    --junit-xml="$JUNIT_PATH"

  # ---- G5 again, AFTER pytest: closes residual 4 (a test that installed or
  # upgraded a package leaves the lock violated; the run must not pass). ----
  check_environments post

  # ---- the zero-skip check and the per-file coverage check run INSIDE
  # this same script, so CI and local apply IDENTICAL gates. ----
  echo "🚫 Enforcing zero-skip policy..."
  "$PYTHON" scripts/check-no-skipped-tests.py "$JUNIT_PATH"

  echo "🔒 Enforcing per-file coverage gate (each component >= 90%)..."
  "$PYTHON" scripts/check-per-file-coverage.py
fi

# ---- the reject log is enforcement, not a warning — either tool. ----
REJECT_CALLS="$(wc -l < "$REJECT_LOG" 2>/dev/null || echo 0)"
if [ "$REJECT_CALLS" -gt 0 ]; then
  echo "❌ the sentinel kubectl/helm shim rejected $REJECT_CALLS call(s) during this run — a hermeticity leak reached a REAL cluster tool. Calls:" >&2
  cat "$REJECT_LOG" >&2
  exit 1
fi

echo "✅ Tests passed (scripts/ci/run-tests.sh)"
