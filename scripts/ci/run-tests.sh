#!/usr/bin/env bash
# scripts/ci/run-tests.sh — THE canonical test-suite invocation.
#
# SPEC 2026-10-11-SPEC-test-parity-scripted-enforcement.md (R1/R4) +
# ADDENDUM A1 (2026-10-11, after an independent review REJECT of f72cdd7),
# written after PR #380 / CI run 37878027812: `make test` (Makefile) and
# the ci.yml `test` job had grown into two hand-typed `pytest tests/ ...`
# command lines that had already drifted. Both now call THIS script
# instead of typing their own pytest invocation over the whole `tests/`
# directory — tests/test_ci_make_test_parity.py (R2/A1-3) mechanically
# asserts that, across EVERY ci.yml job and EVERY Makefile target.
#
# Usage:
#   scripts/ci/run-tests.sh                  # the full gate: --cov, --cov-fail-under=90, junit.xml
#   scripts/ci/run-tests.sh --subset "<expr>" [--junit <path>]
#                                             # A1-3: `make test-unit`'s fast subset mode —
#                                             # same CI shape, own junit, own zero-skip
#                                             # check, but no --cov enforcement (a partial
#                                             # run can't meaningfully satisfy the gate).
#
# R4 — this script forces the CI shape (no reachable cluster) on EVERY
# invocation, laptop or CI alike, in BOTH modes:
#   * KUBECONFIG is pointed at a path that does not exist.
#   * A shim directory, placed FIRST on PATH, ships a `kubectl` that always
#     exits non-zero and logs every call it receives — the exact behaviour
#     a cluster-less CI runner's `kubectl` would produce, except
#     deterministic and auditable. `helm` is left as whatever is really on
#     PATH: `helm template`/`lint`/`dependency`/`version`/`show` render
#     charts and read local files only, no cluster, and some of our chart
#     tests run those for real on purpose.
#
# ADDENDUM A1-4: the shim does not just warn — if its call log is
# non-empty after pytest finishes, THIS SCRIPT EXITS NON-ZERO and prints
# every call it caught. tests/test_run_tests_sh_enforcement.py proves this
# against a scratch copy of this script with a deliberately-leaking fake
# pytest standing in for the real one.
#
# A test that depends on the real cluster must stub its subprocess seam
# (e.g. `DeployRunner(..., mesh_gate=_healthy_gate())`, the pattern already
# used throughout tests/test_deploy_runner.py) — it must never pass only
# because the machine running it happens to have a live k3s cluster. The
# autouse guard in tests/conftest.py (R3, with ADDENDUM A1-1/A1-2's
# committed tests in tests/test_hermetic_cluster_tool_guard.py) intercepts
# every spawn entry point that guard's own test file exercises, before a
# real process is created; this script's kubectl shim + exit-non-zero
# behaviour is the enforcement layer for anything that still reaches a
# real `kubectl` regardless (A1-4 — this paragraph states only what the
# two referenced test files measure).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# ---- argument parsing (A1-3 subset mode) ----
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
    echo "❌ $name not found (.venv/bin/$name or PATH) — install dev deps first (pip install -e '.[dev]')" >&2
    exit 1
  fi
}

PYTEST="$(resolve_tool pytest)"
PYTHON="$(resolve_tool python)"

# ---- R4: force the CI shape — no cluster reachable, ever, from this script ----
SHIM_DIR="$(mktemp -d "${TMPDIR:-/tmp}/audittrace-run-tests-shim.XXXXXX")"
SHIM_LOG="$SHIM_DIR/kubectl-calls.log"
: > "$SHIM_LOG"
export AUDITTRACE_TEST_KUBECTL_SHIM_LOG="$SHIM_LOG"

cat > "$SHIM_DIR/kubectl" <<SHIM_EOF
#!/usr/bin/env bash
# Sentinel kubectl (scripts/ci/run-tests.sh, R4/A1-4). A test that reaches
# this leaked past the tests/conftest.py R3 guard — it is touching a real
# cluster tool from inside the test suite. Log the call and ALWAYS fail,
# exactly like a CI runner that has no cluster at all. A non-empty log
# makes THIS SCRIPT exit non-zero below, not just warn.
echo "\$(date -Iseconds) \$*" >> "$SHIM_LOG"
exit 17
SHIM_EOF
chmod +x "$SHIM_DIR/kubectl"

export KUBECONFIG="$SHIM_DIR/nonexistent-kubeconfig"
export PATH="$SHIM_DIR:$PATH"

cleanup() { rm -rf "$SHIM_DIR"; }
trap cleanup EXIT

echo "run-tests: forcing CI shape — KUBECONFIG=$KUBECONFIG (does not exist), kubectl shim at $SHIM_DIR/kubectl"

if [ -n "$SUBSET_EXPR" ]; then
  # ---- A1-3 subset mode (make test-unit): a fast, filtered run over the
  # SAME tests/ directory, in the SAME CI shape, with its OWN junit and
  # its OWN zero-skip check — but no --cov-fail-under enforcement (a
  # filtered subset can't meaningfully satisfy the per-file gate). ----
  echo "run-tests: subset mode -k \"$SUBSET_EXPR\" -> $JUNIT_PATH"
  "$PYTEST" tests/ -v -k "$SUBSET_EXPR" --no-cov --junit-xml="$JUNIT_PATH"

  echo "🚫 Enforcing zero-skip policy on the subset run..."
  "$PYTHON" scripts/check-no-skipped-tests.py "$JUNIT_PATH"
else
  # ---- R1: THE pytest invocation — the --cov set, the reports,
  # --cov-fail-under, --junit-xml. Nothing else in this repo may hand-type
  # this line. ----
  "$PYTEST" tests/ -v \
    --cov=src \
    --cov=bff \
    --cov=scripts/deploy \
    --cov=scripts/hooks \
    --cov=scripts/release \
    --cov=scripts/migrate \
    --cov=scripts/curator \
    --cov=scripts/network \
    --cov=scripts/neuter \
    --cov=scripts.replay_dead_lettered_index \
    --cov=scripts.backfill_pdf_signature_status \
    --cov=scripts.integration_gate \
    --cov-report=term-missing \
    --cov-report=xml \
    --cov-report=html:htmlcov \
    --cov-fail-under=90 \
    --junit-xml="$JUNIT_PATH"

  # ---- R4: the zero-skip check and the per-file coverage check run
  # INSIDE this same script, so CI and local apply IDENTICAL gates (CI
  # previously had its own inline skip-check heredoc; it now calls this
  # script instead). ----
  echo "🚫 Enforcing zero-skip policy..."
  "$PYTHON" scripts/check-no-skipped-tests.py "$JUNIT_PATH"

  echo "🔒 Enforcing per-file coverage gate (each component >= 90%)..."
  "$PYTHON" scripts/check-per-file-coverage.py
fi

# ---- A1-4: the sentinel kubectl log is enforcement, not a warning. ----
SHIM_CALLS="$(wc -l < "$SHIM_LOG" 2>/dev/null || echo 0)"
if [ "$SHIM_CALLS" -gt 0 ]; then
  echo "❌ sentinel kubectl was invoked $SHIM_CALLS time(s) during this run — a hermeticity leak reached a REAL cluster tool. Calls:" >&2
  cat "$SHIM_LOG" >&2
  exit 1
fi

echo "✅ Tests passed (scripts/ci/run-tests.sh)"
