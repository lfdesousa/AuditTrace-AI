#!/usr/bin/env bash
# scripts/ci/check-images-match-locks.sh — AC-6 (DEP-LOCK-AT WU-2, ADR-065).
#
# Build the three first-party images FROM SOURCE (build-not-pull: never pull an
# unpublished release tag, AUDITTRACE-RELEASE §3) and prove that the `freeze
# --all` listing INSIDE each image equals the lock COMMITTED at HEAD (`git show
# HEAD:<lock>`, not the working tree), plus the VCS package at its pyproject SHA.
#
#   memory-server  Dockerfile                 requirements.txt             (+ audittrace-ai, audittrace-object-storage @ SHA)
#   bff            bff/Dockerfile             bff/requirements.txt
#   llm-stub       images/llm-stub/Dockerfile images/llm-stub/requirements.txt
#
# Fails closed: a missing docker, a failed build, a failed `git show`, an
# unparseable freeze line or any lock/freeze difference exits non-zero. There is
# no flag or environment variable that weakens the comparison.
#
# Usage: scripts/ci/check-images-match-locks.sh [--no-build]
#   --no-build   compare already-built images (same tags); the CI job never uses it.
# Env (naming only, never a comparison switch):
#   AC6_TAG      local tag suffix (default "ac6"); images are audittrace-<x>:$AC6_TAG
#   AC6_PLATFORM optional `--platform` for build and run (AC-7a: linux/arm64)
#   AC6_PYTHON   interpreter with `packaging` (default .venv/bin/python, else python3)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

BUILD=1
case "${1:-}" in
  "") ;;
  --no-build) BUILD=0 ;;
  *) echo "❌ check-images-match-locks: unknown argument $1" >&2; exit 2 ;;
esac

TAG="${AC6_TAG:-ac6}"
PLATFORM_ARGS=()
if [ -n "${AC6_PLATFORM:-}" ]; then
  PLATFORM_ARGS=(--platform "$AC6_PLATFORM")
fi

command -v docker >/dev/null 2>&1 || { echo "❌ docker not found" >&2; exit 1; }
if [ -n "${AC6_PYTHON:-}" ]; then
  PY="$AC6_PYTHON"
elif [ -x .venv/bin/python ]; then
  PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "❌ no python found (AC6_PYTHON, .venv/bin/python or python3)" >&2; exit 1
fi

# Always compare the COMMITTED bytes: a dirty working-tree lock must not pass.
git rev-parse --verify HEAD >/dev/null

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/audittrace-ac6.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT

build() { # build <image> <dockerfile> <context>
  docker build ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} -f "$2" -t "$1" "$3"
}

# The one distribution every image carries that is NOT in its lock: the base
# image's own installer (python:3.12-slim ships it; the runtime locks do not
# name it). Allowed by name, version not asserted here: it is recorded in the
# build evidence and the unpinned base-image tag is a declared residual (BL-3).
BASE_IMAGE_TOOLING=(--allow-extra-pinned pip)

check() { # check <label> <image> <lock path> [extra check_freeze args...]
  local label="$1" image="$2" lock="$3"
  shift 3
  # The image's own python and its own marker environment (arch-correct).
  docker run --rm ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} --entrypoint python "$image" -c \
    'import json; from pip._vendor.packaging.markers import default_environment as d; print(json.dumps(d()))' \
    > "$SCRATCH/$label.markers.json"
  docker run --rm ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} --entrypoint python "$image" -m pip freeze --all \
    > "$SCRATCH/$label.freeze"
  "$PY" scripts/ci/check_freeze_matches_lock.py \
    --lock-rev "HEAD:$lock" \
    --freeze "$SCRATCH/$label.freeze" \
    --marker-env "$SCRATCH/$label.markers.json" \
    --label "$label image ($image)" \
    "${BASE_IMAGE_TOOLING[@]}" \
    "$@"
}

MS="audittrace-memory-server:$TAG"
BFF="audittrace-bff:$TAG"
STUB="audittrace-llm-stub:$TAG"

if [ "$BUILD" -eq 1 ]; then
  build "$MS" Dockerfile .
  build "$BFF" bff/Dockerfile .
  build "$STUB" images/llm-stub/Dockerfile images/llm-stub
fi

# Every image is judged (no early exit), then the worst result decides.
RC=0
check memory-server "$MS" requirements.txt \
  --allow-extra audittrace-ai --allow-extra audittrace-object-storage \
  --vcs-package audittrace-object-storage --pyproject pyproject.toml || RC=1
check bff "$BFF" bff/requirements.txt || RC=1
check llm-stub "$STUB" images/llm-stub/requirements.txt || RC=1

if [ "$RC" -ne 0 ]; then
  echo "❌ AC-6: at least one image differs from its committed lock (see above)" >&2
  exit 1
fi
echo "✅ AC-6: freeze listing == git show HEAD:<lock> for memory-server, bff, llm-stub (tag $TAG)"
