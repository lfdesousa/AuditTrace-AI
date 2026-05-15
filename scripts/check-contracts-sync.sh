#!/usr/bin/env bash
# Cross-repo contract drift detector.
#
# Fetches the canonical contracts/v1.yaml from audittrace-content-control's
# default branch and byte-diffs it against AT-AI's vendored copy at
# docs/reference/content-control/contracts/v1.yaml. Exits non-zero on drift.
#
# The vendored copy is treated as a snapshot of the canonical upstream.
# B5 PR-B12's tests/test_content_control_contract.py asserts the vendored
# file's *shape* matches AT-AI's producer code at test time; this script
# closes the second half of the gap — that the vendored file is the
# CURRENT canonical, not a stale snapshot.
#
# Failure mode this catches: cc lands a contract change, vendored copy
# stays stale, drift is silent until someone notices at runtime.
#
# Usage:
#   scripts/check-contracts-sync.sh
#
# Auth: audittrace-content-control is a PRIVATE repo, so the
# raw fetch needs a token with at least ``repo:contents:read`` on
# that repo. Provide it via ``GH_TOKEN`` (or ``GITHUB_TOKEN`` —
# checked second). When neither is set the script tries anonymous
# and fails on 404. CI passes ``secrets.CONTRACT_SYNC_TOKEN`` →
# ``GH_TOKEN``; locally, ``export GH_TOKEN=$(cat ~/work/audittrace-private/secrets/ghcr-pat-cluster.txt)``.
#
# CI wiring: .github/workflows/contract-sync.yml runs this on every PR,
# every push to main, and once a day on cron.

set -euo pipefail

CC_REPO="lfdesousa/audittrace-content-control"
CC_BRANCH="main"
CC_PATH="contracts/v1.yaml"
VENDORED_PATH="docs/reference/content-control/contracts/v1.yaml"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_FILE="${REPO_ROOT}/${VENDORED_PATH}"

if [ ! -f "${LOCAL_FILE}" ]; then
    echo "✗ vendored copy missing: ${LOCAL_FILE}" >&2
    exit 2
fi

UPSTREAM_URL="https://raw.githubusercontent.com/${CC_REPO}/${CC_BRANCH}/${CC_PATH}"
TMP_FILE="$(mktemp -t cc-canonical-v1.XXXXXX.yaml)"
trap 'rm -f "${TMP_FILE}"' EXIT

TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
AUTH_ARGS=()
if [ -n "${TOKEN}" ]; then
    AUTH_ARGS=(-H "Authorization: Bearer ${TOKEN}")
fi

# 3 attempts with short backoff to weather a transient GitHub flake.
# Fail hard on a final miss — silent fallback would mask drift.
for attempt in 1 2 3; do
    if curl -fsSL --max-time 15 "${AUTH_ARGS[@]}" -o "${TMP_FILE}" "${UPSTREAM_URL}"; then
        break
    fi
    if [ "${attempt}" -eq 3 ]; then
        echo "✗ failed to fetch ${UPSTREAM_URL} after 3 attempts" >&2
        if [ -z "${TOKEN}" ]; then
            echo "  (no GH_TOKEN/GITHUB_TOKEN set; cc-repo is private — 404 expected without auth)" >&2
        fi
        exit 3
    fi
    sleep $((attempt * 2))
done

LOCAL_SHA="$(sha256sum "${LOCAL_FILE}" | awk '{print $1}')"
UPSTREAM_SHA="$(sha256sum "${TMP_FILE}" | awk '{print $1}')"

echo "vendored : ${LOCAL_FILE}"
echo "  sha256 : ${LOCAL_SHA}"
echo "canonical: ${UPSTREAM_URL}"
echo "  sha256 : ${UPSTREAM_SHA}"

if [ "${LOCAL_SHA}" = "${UPSTREAM_SHA}" ]; then
    echo "✓ contracts/v1.yaml is in sync with cc-repo ${CC_BRANCH}"
    exit 0
fi

echo "✗ DRIFT: AT-AI's vendored contracts/v1.yaml does not match cc-repo's canonical."
echo ""
echo "Diff (vendored vs canonical):"
diff -u "${LOCAL_FILE}" "${TMP_FILE}" || true
echo ""
echo "To resolve:"
echo "  1. Decide whether the cc change is intended for AT-AI."
echo "  2. If yes: copy the upstream into the vendored path and open a sync PR."
echo "       curl -fsSL ${UPSTREAM_URL} > ${VENDORED_PATH}"
echo "  3. If no: revert the cc change OR pin AT-AI to a tagged contract"
echo "       version (out of scope for this guard today)."
exit 1
