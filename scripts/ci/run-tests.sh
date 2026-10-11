#!/usr/bin/env bash
# scripts/ci/run-tests.sh — THE canonical test-suite invocation.
#
# SPEC 2026-10-11-SPEC-test-parity-scripted-enforcement.md (R1/R4), written
# after PR #380 / CI run 37878027812: `make test` (Makefile) and the ci.yml
# `test` job had grown into two hand-typed `pytest tests/ ...` command
# lines that had already drifted (the Makefile covered `bff`,
# `scripts/network`, `scripts/neuter` and `scripts.integration_gate`; CI
# did not). Both now call THIS script instead of typing their own pytest
# invocation — tests/test_ci_make_test_parity.py (R2) mechanically asserts
# that neither one still does.
#
# R4 — this script forces the CI shape (no reachable cluster) on EVERY
# invocation, laptop or CI alike:
#   * KUBECONFIG is pointed at a path that does not exist.
#   * A shim directory, placed FIRST on PATH, ships a `kubectl` that always
#     exits non-zero and logs every call it receives — the exact behaviour
#     a cluster-less CI runner's `kubectl` would produce, except
#     deterministic and auditable. `helm` is left as whatever is really on
#     PATH: `helm template`/`lint`/`dependency`/`version`/`show` render
#     charts and read local files only, no cluster, and some of our chart
#     tests run those for real on purpose.
#
# A test that depends on the real cluster must stub its subprocess seam
# (e.g. `DeployRunner(..., mesh_gate=_healthy_gate())`, the pattern already
# used throughout tests/test_deploy_runner.py) — it must never pass only
# because the machine running it happens to have a live k3s cluster. The
# autouse guard in tests/conftest.py (R3) catches most such leaks at the
# Python level, before a real process is ever spawned; this script's
# kubectl shim is the backstop for anything that slips past it (a nested
# shell script, for instance).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

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
# Sentinel kubectl (scripts/ci/run-tests.sh, R4). A test that reaches this
# leaked past the tests/conftest.py R3 guard — it is touching a real
# cluster tool from inside the test suite. Log the call and ALWAYS fail,
# exactly like a CI runner that has no cluster at all.
echo "\$(date -Iseconds) \$*" >> "$SHIM_LOG"
exit 17
SHIM_EOF
chmod +x "$SHIM_DIR/kubectl"

export KUBECONFIG="$SHIM_DIR/nonexistent-kubeconfig"
export PATH="$SHIM_DIR:$PATH"

cleanup() {
  local calls
  calls="$(wc -l < "$SHIM_LOG" 2>/dev/null || echo 0)"
  if [ "$calls" -gt 0 ]; then
    echo "⚠️  sentinel kubectl was invoked $calls time(s) — see $SHIM_LOG" >&2
    cat "$SHIM_LOG" >&2
  fi
  rm -rf "$SHIM_DIR"
}
trap cleanup EXIT

echo "run-tests: forcing CI shape — KUBECONFIG=$KUBECONFIG (does not exist), kubectl shim at $SHIM_DIR/kubectl"

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
  --junit-xml=junit.xml

# ---- R4: the zero-skip check and the per-file coverage check run INSIDE
# this same script, so CI and local apply IDENTICAL gates (CI previously
# had its own inline skip-check heredoc; it now calls this script instead). ----
echo "🚫 Enforcing zero-skip policy..."
"$PYTHON" scripts/check-no-skipped-tests.py

echo "🔒 Enforcing per-file coverage gate (each component >= 90%)..."
"$PYTHON" scripts/check-per-file-coverage.py

echo "✅ Tests passed (scripts/ci/run-tests.sh)"
