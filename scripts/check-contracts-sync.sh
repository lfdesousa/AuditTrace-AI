#!/usr/bin/env bash
# Vendored-contract pin check (local, offline, tokenless).
#
# AT-AI vendors a copy of audittrace-content-control's canonical
# contracts/v1.yaml at docs/reference/content-control/contracts/v1.yaml and
# records where it came from in the sibling pin file v1.yaml.source
# (repo, full commit sha, sha256 of the file). This script verifies that the
# vendored file's sha256 equals the pinned sha256.
#
# This public repo NEVER fetches the private content-control repo. The drift
# detector (canonical vs vendored) is the symmetric guard on the private
# side: content-control's .github/workflows/contract-sync.yml reads THIS
# repo's public vendored copy anonymously and diffs it against its canonical.
#
# What this check buys: an accidental local edit of the vendored copy fails
# here, and re-vendoring is a deliberate act (update the file AND the pin).
# B5 PR-B12's tests/test_content_control_contract.py separately asserts the
# vendored file's *shape* against AT-AI's producer code.
#
# Usage:
#   scripts/check-contracts-sync.sh
#
# Exit codes (each with a named message on stderr/stdout):
#   0  vendored sha256 equals the pin
#   1  MISMATCH: vendored file differs from the pin
#   2  vendored file missing
#   3  pin file missing
#   4  pin file malformed (unknown/duplicate/missing key, bad format)
#
# Portability: CONTRACTS_SYNC_ROOT overrides the repo root (default: the
# repository containing this script); used by the tests to drive fixtures.
#
# CI wiring: .github/workflows/contract-sync.yml (no token, no network).

set -euo pipefail

VENDORED_PATH="docs/reference/content-control/contracts/v1.yaml"
PIN_PATH="${VENDORED_PATH}.source"

REPO_ROOT="${CONTRACTS_SYNC_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOCAL_FILE="${REPO_ROOT}/${VENDORED_PATH}"
PIN_FILE="${REPO_ROOT}/${PIN_PATH}"

if [ ! -f "${LOCAL_FILE}" ]; then
    echo "✗ vendored copy missing: ${LOCAL_FILE}" >&2
    exit 2
fi
if [ ! -f "${PIN_FILE}" ]; then
    echo "✗ pin file missing: ${PIN_FILE}" >&2
    echo "  (record repo, commit and sha256 next to the vendored copy)" >&2
    exit 3
fi

# Parse the pin as DATA (never sourced). Accepted lines: blank, '#' comment,
# or exactly one of repo=, commit=, sha256= (each once).
PIN_REPO="" PIN_COMMIT="" PIN_SHA=""
while IFS= read -r line || [ -n "${line}" ]; do
    case "${line}" in
        "" | "#"*) continue ;;
        repo=*)
            [ -z "${PIN_REPO}" ] || { echo "✗ pin malformed: duplicate repo" >&2; exit 4; }
            PIN_REPO="${line#repo=}" ;;
        commit=*)
            [ -z "${PIN_COMMIT}" ] || { echo "✗ pin malformed: duplicate commit" >&2; exit 4; }
            PIN_COMMIT="${line#commit=}" ;;
        sha256=*)
            [ -z "${PIN_SHA}" ] || { echo "✗ pin malformed: duplicate sha256" >&2; exit 4; }
            PIN_SHA="${line#sha256=}" ;;
        *)
            echo "✗ pin malformed: unrecognised line in ${PIN_FILE}" >&2
            exit 4 ;;
    esac
done <"${PIN_FILE}"

if ! [[ "${PIN_REPO}" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]]; then
    echo "✗ pin malformed: repo must be <owner>/<name>" >&2
    exit 4
fi
if ! [[ "${PIN_COMMIT}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "✗ pin malformed: commit must be a full 40-hex sha" >&2
    exit 4
fi
if ! [[ "${PIN_SHA}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "✗ pin malformed: sha256 must be 64 lowercase hex" >&2
    exit 4
fi

LOCAL_SHA="$(sha256sum "${LOCAL_FILE}" | awk '{print $1}')"

echo "vendored : ${VENDORED_PATH}"
echo "  sha256 : ${LOCAL_SHA}"
echo "pinned   : ${PIN_REPO}@${PIN_COMMIT}"
echo "  sha256 : ${PIN_SHA}"

if [ "${LOCAL_SHA}" = "${PIN_SHA}" ]; then
    echo "✓ vendored contracts/v1.yaml matches its pin"
    exit 0
fi

echo "✗ MISMATCH: the vendored contracts/v1.yaml does not match its pin." >&2
echo "" >&2
echo "To resolve:" >&2
echo "  - accidental edit: git checkout -- ${VENDORED_PATH}" >&2
echo "  - deliberate re-vendor: copy the canonical file from ${PIN_REPO}" >&2
echo "    at the new commit AND update ${PIN_PATH} (commit + sha256) in the same change." >&2
exit 1
