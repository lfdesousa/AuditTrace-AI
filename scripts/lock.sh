#!/usr/bin/env bash
# scripts/lock.sh — THE one place the hashed locks are compiled, checked and
# upgraded (DEP-LOCK-AT, ADR-065). `make lock | lock-check | lock-freshness |
# lock-upgrade` are thin wrappers around this script, so the lock matrix and
# the compiler flags exist exactly once.
#
# Usage:
#   scripts/lock.sh compile            # (re)write every lock from its inputs
#   scripts/lock.sh check              # G1: fixed point under the committed pins, hashes re-derived; exit 1 on drift
#   scripts/lock.sh freshness          # G2: informational --upgrade diff
#   scripts/lock.sh upgrade [PKG]      # deliberate upgrade (all, or one package)
#
# Compiler: uv (operator ruling Q1, spec §4.1) pinned in the dev lock. Tool
# resolution follows CI-PARITY §2: .venv/bin/uv if executable, else uv on PATH,
# else FAIL CLOSED. `env -u SSL_CERT_FILE` because the operator's private CA
# bundle shadows the system roots for uv's rustls (reference_ssl_cert_file_
# shadows_system_ca).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---- tool resolution (fail closed) ----
UV="$( [ -x .venv/bin/uv ] && echo .venv/bin/uv || command -v uv || true )"
if [ -z "$UV" ]; then
  echo "❌ uv not found (looked for .venv/bin/uv and uv on PATH)." >&2
  echo "   uv is the lock compiler, pinned in requirements-dev.txt. Provision it:" >&2
  echo "       make venv                                        # laptop" >&2
  echo "       bash scripts/install-locked.sh pip uv-only       # CI / any python" >&2
  exit 1
fi

PYVER="$(tr -d '[:space:]' < .python-version)"
if [ -z "$PYVER" ]; then
  echo "❌ .python-version is empty — it is the single source for the lock's --python-version." >&2
  exit 1
fi

# ---- the lock matrix: ONE definition. name|output|inputs ----
# dev is constrained by the runtime lock (-c) so shared packages resolve to the
# identical version; bff likewise. The stub keeps its own old fastapi on purpose.
NO_EMIT="--no-emit-package audittrace-object-storage"
LOCKS=(
  "requirements.txt|pyproject.toml requirements-build.in $NO_EMIT"
  "requirements-dev.txt|pyproject.toml requirements-build.in --extra dev -c requirements.txt $NO_EMIT"
  "bff/requirements.txt|bff/requirements.in -c requirements.txt"
  "images/llm-stub/requirements.txt|images/llm-stub/requirements.in"
  "requirements-gate.txt|requirements-gate.in"
)

compile_one() {
  # compile_one <output> <inputs...> [extra flags...]
  local out="$1"; shift
  # shellcheck disable=SC2086
  env -u SSL_CERT_FILE "$UV" pip compile "$@" \
    --universal --generate-hashes --allow-unsafe \
    --python-version "$PYVER" -o "$out" --quiet
}

strip_comments() { grep -vE '^[[:space:]]*#' "$1"; }

cmd="${1:-}"
case "$cmd" in
  compile)
    for row in "${LOCKS[@]}"; do
      out="${row%%|*}"; inputs="${row#*|}"
      echo "lock: compiling $out"
      # shellcheck disable=SC2086
      compile_one "$out" $inputs
    done
    ;;
  check)
    # G1 — fixed point of the inputs UNDER THE COMMITTED VERSIONS, with every
    # hash re-derived from the index. Measured 2026-10-19 (neuter N1): a
    # SEEDED recompile (copy the lock, `-o` into the copy) is NOT enough with
    # uv — uv trusts the hash lines already in the output file, so a lock with
    # one hash turned to 64 zeros reproduces itself byte-for-byte and the check
    # stays green. So the committed lock is reduced to a versions-only
    # constraints file (`name==version [; marker]`, hashes dropped), and the
    # inputs are compiled FRESH (no output file to trust) under it: the pins
    # cannot move (constraints), every hash comes from the index. The
    # comment-stripped diff must be empty. Consequences: a tampered/missing
    # hash is RED (N1); a direct pin bumped in pyproject without `make lock`
    # contradicts its own constraint and fails to resolve (N2); a stale pin for
    # a dependency that was removed is RED. Residual: if PyPI later adds a file
    # to an already-locked release, the fresh compile gains a hash and this goes
    # RED until `make lock` — a real change in the hash set, fixed by one command.
    work="$(mktemp -d "${TMPDIR:-/tmp}/audittrace-lock-check.XXXXXX")"
    trap 'rm -rf "$work"' EXIT
    status=0
    for row in "${LOCKS[@]}"; do
      out="${row%%|*}"; inputs="${row#*|}"
      tmp="$work/$(echo "$out" | tr '/' '_')"
      # versions-only constraints: pin lines, trailing line-continuation removed
      grep -E '^[A-Za-z0-9][A-Za-z0-9._-]*==' "$out" | sed -E 's/[[:space:]]*\\$//' > "$tmp.constraints"
      # shellcheck disable=SC2086
      if ! compile_one "$tmp" $inputs -c "$tmp.constraints" 2>"$tmp.err"; then
        echo "❌ lock-check: $out cannot be recompiled under its own pins:" >&2
        head -20 "$tmp.err" >&2
        status=1
        continue
      fi
      if diff <(strip_comments "$out") <(strip_comments "$tmp") >"$tmp.diff"; then
        echo "lock-check: $out OK (fixed point, hashes re-derived)"
      else
        echo "❌ lock-check: $out is NOT the fixed point of its inputs:" >&2
        head -40 "$tmp.diff" >&2
        status=1
      fi
    done
    if [ "$status" -ne 0 ]; then
      echo "   Fix: edit the inputs deliberately, then run 'make lock' (or 'make lock-upgrade') and commit the result." >&2
    fi
    exit "$status"
    ;;
  freshness)
    # G2 — informational: what would --upgrade change? Never gates anything.
    work="$(mktemp -d "${TMPDIR:-/tmp}/audittrace-lock-freshness.XXXXXX")"
    trap 'rm -rf "$work"' EXIT
    for row in "${LOCKS[@]}"; do
      out="${row%%|*}"; inputs="${row#*|}"
      tmp="$work/$(echo "$out" | tr '/' '_')"
      cp "$out" "$tmp"
      # shellcheck disable=SC2086
      compile_one "$tmp" $inputs --upgrade
      echo "### $out"
      diff <(strip_comments "$out" | grep -E '^[A-Za-z0-9_.\-]+(\[[^]]*\])?==') \
           <(strip_comments "$tmp" | grep -E '^[A-Za-z0-9_.\-]+(\[[^]]*\])?==') || true
    done
    ;;
  upgrade)
    pkg="${2:-}"
    for row in "${LOCKS[@]}"; do
      out="${row%%|*}"; inputs="${row#*|}"
      echo "lock: upgrading $out ${pkg:+(package $pkg)}"
      if [ -n "$pkg" ]; then
        # shellcheck disable=SC2086
        compile_one "$out" $inputs --upgrade-package "$pkg"
      else
        # shellcheck disable=SC2086
        compile_one "$out" $inputs --upgrade
      fi
    done
    ;;
  *)
    echo "usage: scripts/lock.sh compile|check|freshness|upgrade [PKG]" >&2
    exit 2
    ;;
esac
