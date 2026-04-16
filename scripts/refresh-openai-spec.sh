#!/usr/bin/env bash
# Refresh the vendored OpenAI OpenAPI spec in docs/reference/openai/.
#
# Pulls the current authoritative spec from Stainless (the version
# OpenAI themselves link to from github.com/openai/openai-openapi) and
# updates the "Pulled on" date in the folder README. Review the diff
# and commit.
#
# Run periodically (~monthly) or after OpenAI announces a spec
# change. Strict OpenAI /v1/chat/completions compatibility is
# AuditTrace-AI's biggest integration asset (see
# docs/reference/openai/README.md and
# feedback_openai_schema_inviolate memory).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPEC_DIR="${REPO_ROOT}/docs/reference/openai"
SPEC_FILE="${SPEC_DIR}/openapi.yaml"
README="${SPEC_DIR}/README.md"
STAINLESS_URL="https://app.stainless.com/api/spec/documented/openai/openapi.documented.yml"
TODAY="$(date -u +%Y-%m-%d)"

echo "Refreshing OpenAI OpenAPI spec from Stainless..."
echo "  URL:    ${STAINLESS_URL}"
echo "  Target: ${SPEC_FILE}"

# Download to a temp file first so a failed pull doesn't nuke the
# previous vendored copy.
TMP="$(mktemp)"
trap 'rm -f "${TMP}"' EXIT

curl -sSL --fail -o "${TMP}" "${STAINLESS_URL}"

# Sanity: at least 1MB and contains the chat-completions path.
if [[ $(stat -c '%s' "${TMP}") -lt 1000000 ]]; then
    echo "ERROR: downloaded spec is suspiciously small (<1MB). Aborting." >&2
    exit 1
fi
if ! grep -q '/chat/completions:' "${TMP}"; then
    echo "ERROR: downloaded spec does not contain /chat/completions path." >&2
    exit 1
fi

mv "${TMP}" "${SPEC_FILE}"

# Update the "Pulled on" date in the folder README.
if [[ -f "${README}" ]]; then
    # Replace the ISO date in the table row that references openapi.yaml.
    sed -i -E \
        "s|(\`openapi\.yaml\` \|[^|]+\| )[0-9]{4}-[0-9]{2}-[0-9]{2}|\1${TODAY}|" \
        "${README}"
fi

LINES=$(wc -l < "${SPEC_FILE}")
SIZE=$(stat -c '%s' "${SPEC_FILE}")
echo "OK: ${SPEC_FILE} refreshed — ${LINES} lines, ${SIZE} bytes."
echo "Review the diff and commit:"
echo "  git diff docs/reference/openai/"
