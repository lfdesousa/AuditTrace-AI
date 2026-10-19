#!/usr/bin/env bash
# scripts/install-locked.sh — THE chokepoint for installing AuditTrace-AI's
# Python dependencies from the hashed locks (DEP-LOCK-AT, ADR-065).
#
# Usage: scripts/install-locked.sh <pip> [dev|gate|uv-only]
#   <pip>   the pip to drive: `.venv/bin/pip` (laptop), `pip` (CI runner python,
#           no .venv — CI-PARITY §1), `.gate-venv/bin/pip`. May be several
#           words (e.g. "python -m pip").
#   dev     (default) requirements-dev.txt, then `-e .`, then the VCS package.
#   gate    requirements-gate.txt only (the isolated semgrep venv, C7).
#   uv-only just the hashed uv line of requirements-dev.txt (lock-check CI job).
#
# Steps, in this order, each with NO fallback (spec C1):
#   1. pip itself from its HASHED line in the lock — never `--upgrade pip`.
#   2. `--require-hashes --only-binary=:all: -r <lock>`.
#   3. `--no-deps --no-build-isolation -e .`            (dev only; first removes
#      stale src/*.egg-info, which would shadow the venv's dist-info — B2)
#   4. the VCS package at the commit SHA read from pyproject.toml
#      (`--no-deps --no-build-isolation`; git has no hashes — a commit SHA is
#      content-addressed, spec §4.4)                      (dev only)
# `--no-build-isolation` means setuptools/wheel come from the hashed lock, not
# an unpinned PyPI fetch into a throw-away build env (B1).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PIP="${1:?usage: install-locked.sh <pip> [dev|gate|uv-only]}"
MODE="${2:-dev}"

case "$MODE" in
  dev|uv-only) LOCK="requirements-dev.txt" ;;
  gate)        LOCK="requirements-gate.txt" ;;
  *) echo "❌ install-locked.sh: unknown mode '$MODE' (dev|gate|uv-only)" >&2; exit 2 ;;
esac

work="$(mktemp -d "${TMPDIR:-/tmp}/audittrace-install-locked.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# block_of <name> <lock> -> that one pinned package with its hash lines only.
block_of() {
  awk -v want="$1" '
    $0 ~ "^" want "==" { p = 1; print; next }
    p && /^[^ ]/ { exit }
    p && /--hash=/ { print }
  ' "$2"
}

block="$work/pip.txt"
block_of pip "$LOCK" > "$block"
if [ ! -s "$block" ]; then
  echo "❌ install-locked.sh: no hashed 'pip==' line in $LOCK (Q5 requires pip pinned in the lock)." >&2
  exit 1
fi
# shellcheck disable=SC2086
$PIP install --require-hashes --only-binary=:all: -r "$block"

if [ "$MODE" = "uv-only" ]; then
  block_of uv "$LOCK" > "$work/uv.txt"
  if [ ! -s "$work/uv.txt" ]; then
    echo "❌ install-locked.sh: no hashed 'uv==' line in $LOCK." >&2
    exit 1
  fi
  # shellcheck disable=SC2086
  $PIP install --require-hashes --only-binary=:all: -r "$work/uv.txt"
  exit 0
fi

# shellcheck disable=SC2086
$PIP install --require-hashes --only-binary=:all: -r "$LOCK"

if [ "$MODE" = "dev" ]; then
  # Review B2: a stale, gitignored src/<pkg>.egg-info (left by an older
  # non-PEP-660 `pip install -e`) is reachable through the editable .pth and
  # shadows the venv's dist-info, so G5 would refuse the fresh venv. Remove
  # ONLY src/*.egg-info (maxdepth 1, name-matched) before the editable install.
  find src -maxdepth 1 -name '*.egg-info' -prune -exec rm -rf {} + 2>/dev/null || true
  # shellcheck disable=SC2086
  $PIP install --no-deps --no-build-isolation -e .
  SHA="$(sed -n 's#.*audittrace-object-storage\.git@\([0-9a-f]\{40\}\)".*#\1#p' pyproject.toml)"
  if [ -z "$SHA" ]; then
    echo "❌ install-locked.sh: no 40-hex audittrace-object-storage commit SHA in pyproject.toml." >&2
    exit 1
  fi
  # shellcheck disable=SC2086
  $PIP install --no-deps --no-build-isolation \
    "audittrace-object-storage @ git+https://github.com/lfdesousa/audittrace-object-storage.git@${SHA}"
fi
