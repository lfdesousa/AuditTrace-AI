#!/usr/bin/env bash
# Pre-commit gate enforcing the Test, Evidence, and
# Reconstructibility Gate (ADR-049 — see
# docs/ADR-049-test-evidence-and-reconstructibility-gate.md).
#
# Refuses commits that touch production code paths
# (src/audittrace/routes/** or src/audittrace/services/**) unless
# the commit message references captured live-system evidence —
# either a PR draft URL, a path to an evidence file, or a trace ID
# matching the typical 32-hex-char shape that OpenTelemetry / Tempo
# produce.
#
# This is the LOCAL layer of the four-layer enforcement stack. The
# others are: PR template (.github/pull_request_template.md), CI
# evidence-check job (.github/workflows/ci.yml), CLAUDE.md +
# AGENTS.md addenda. All four reinforce the same rule; this hook
# catches the failure mode at the earliest possible point — before
# the work even leaves the laptop.
#
# Per ADR-049 §Decision rule 4: NO override mechanism. If you hit
# this hook on a commit you can't justify with evidence, the answer
# is to capture the evidence (deploy + verify), not bypass the
# hook. --no-verify is reserved for true emergencies the user
# explicitly authorises.
#
# Path-skip heuristic: pure docs (docs/**.md), pure tests
# (tests/**), and *.md files in the repo root skip the hook —
# these don't need live-system evidence by their nature. Mixed
# changes (production code + docs) DO trigger the hook.
#
# Run automatically as a pre-commit hook (see
# .pre-commit-config.yaml). Manual invocation accepts a commit
# message file as $1 (mirrors the commit-msg / prepare-commit-msg
# stages).
#
# Exit 0 if clean (or no triggering paths changed); exit 1 if a
# triggering path was changed without an evidence reference in the
# message.

set -euo pipefail

COMMIT_MSG_FILE="${1:-.git/COMMIT_EDITMSG}"

# ----- Stage 1: did this commit touch a triggering path? -----
# Use --cached for the staged set (the standard pre-commit context).
# When invoked outside pre-commit (e.g. manual test), fall back to
# the working-tree diff against HEAD.
if git rev-parse --verify HEAD >/dev/null 2>&1; then
  CHANGED_FILES=$(git diff --cached --name-only --diff-filter=ACMR HEAD)
else
  # Initial commit — diff-tree against an empty tree.
  CHANGED_FILES=$(git diff --cached --name-only --diff-filter=ACMR)
fi

# Filter to triggering paths — production code under
# src/audittrace/routes/ or src/audittrace/services/.
TRIGGERING=$(echo "$CHANGED_FILES" | grep -E '^src/audittrace/(routes|services)/' || true)

if [[ -z "$TRIGGERING" ]]; then
  # Nothing in this commit touches the gated paths. Hook passes.
  exit 0
fi

# ----- Stage 2: parse the commit message for an evidence reference -----
if [[ ! -f "$COMMIT_MSG_FILE" ]]; then
  echo "::error:: ADR-049 evidence gate: commit message file not found at $COMMIT_MSG_FILE" >&2
  exit 1
fi

COMMIT_MSG=$(cat "$COMMIT_MSG_FILE")

# Strip comment lines (git's # comments) before pattern matching.
COMMIT_BODY=$(echo "$COMMIT_MSG" | grep -v '^#' || true)

# Three accepted evidence-reference shapes:
#   1. URL to a GitHub PR (draft or open):
#      https://github.com/<owner>/<repo>/pull/<n>
#   2. Path to an evidence file under ~/work/audittrace-evidence/
#      or evidence/ in the repo root.
#   3. Trace ID — 32 hex chars, the shape OpenTelemetry / Tempo /
#      Langfuse emit. Embedded in a sentence or on its own line.
PATTERN_PR_URL='https://github\.com/[A-Za-z0-9._/-]+/pull/[0-9]+'
PATTERN_EVIDENCE_PATH='(~/work/audittrace-evidence/|evidence/)[A-Za-z0-9._/-]+'
PATTERN_TRACE_ID='\b[a-f0-9]{32}\b'

if echo "$COMMIT_BODY" | grep -qE "$PATTERN_PR_URL|$PATTERN_EVIDENCE_PATH|$PATTERN_TRACE_ID"; then
  # Found at least one evidence reference.
  exit 0
fi

# ----- Stage 3: fail with the self-check message -----
cat >&2 <<'EOF'
::error:: ADR-049 evidence gate FAILED — commit refused.

This commit changes production code in
src/audittrace/routes/ or src/audittrace/services/, which means
the Test + Evidence Gate (ADR-049) requires the commit message
to reference captured live-system evidence. None was found.

Accepted evidence-reference shapes (any one is sufficient):
  1. PR URL                  https://github.com/<owner>/<repo>/pull/<n>
  2. Evidence-file path      ~/work/audittrace-evidence/<...>
                             evidence/<...>
  3. Trace ID                a 32-hex-char OpenTelemetry/Tempo/Langfuse ID

The 5-step self-check before any commit-shape question:
  1. Did I build a new image AND restart the running service AND
     hit the running endpoint with the new code?
  2. Do I have a captured artefact (kubectl output, ChromaDB query,
     Langfuse trace, helm rollout, response body) demonstrating
     the change produced its intended effect?
  3. For infra/config/chart changes: did I verify the
     provisioner-side linkage (the 2026-05-03 corollary)?
  4. For API-touching changes: did I exercise it through the
     public API with a properly-scoped JWT (no kubectl exec
     bypass, no MinIO root creds, no scope-skipping)?
  5. Are the artefacts referenced from the commit body AND from
     the PR body?

If any answer is "no" → fix the friction (deploy, verify, capture)
and amend the commit body with the evidence reference. There is
no override; per ADR-049 §Decision rule 4, the cost of capturing
evidence is paid every time.

See:
  - docs/ADR-049-test-evidence-and-reconstructibility-gate.md
  - .github/pull_request_template.md
  - AGENTS.md "Test + Evidence Gate" section
EOF

exit 1
