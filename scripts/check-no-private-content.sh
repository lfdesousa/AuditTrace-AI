#!/usr/bin/env bash
# Pre-commit gate that flags private/legal content before it lands in
# the public repository.
#
# Catches the recurrent failure mode where customer names, contact
# details, pricing, or other commercially-sensitive substance leaks
# into a public-repo file (ADRs, drift docs, test fixtures, scripts).
# Per `feedback_pitch_public_vs_private`: anything addressed to a
# non-publicly-approaching counterparty stays in
# ~/work/audittrace-private/ or ~/work/pitch-private/, NEVER in this
# repository.
#
# Run automatically as a `pre-commit` hook (see .pre-commit-config.yaml).
# Exit 0 if clean, 1 if any forbidden pattern is found in the staged
# changes.
#
# Usage (manual / debugging):
#     ./scripts/check-no-private-content.sh        # scans staged files
#     SCAN_ALL=1 ./scripts/check-no-private-content.sh   # scans the whole repo
#
# Adding a new forbidden pattern: edit FORBIDDEN_PATTERNS below. Keep
# the patterns case-insensitive and as specific as possible to avoid
# false positives.
#
# Adding an exception: edit EXCLUDED_PATHS below. Vendored upstream
# content (docs/reference/openai/*) is already exempt — that file is
# OpenAI's, not ours, and rewriting it would break the byte-for-byte
# diff against upstream.

set -euo pipefail

# ----- Forbidden patterns (case-insensitive) -----
# Customer / counterparty names that have NOT publicly approached.
# Add or remove names as the engagement matrix evolves. Names of
# parties who DID approach publicly (per feedback_pitch_public_vs_private)
# can stay in `docs/pitch/` and `docs/phd/`; the EXCLUDED_PATHS section
# below permits exactly those subtrees.
FORBIDDEN_PATTERNS=(
  # Private customer names
  'poc-customer'
  'nicola[. ]?gr[uü]tter'
  # Email domains that imply private counterparty contact
  '@poc-customer\.ch'
  # Real-people names of non-publicly-approaching parties
  'federico'                         # ContextShield context
  'flavio'                           # ContextShield context
  # Commercial pricing — CHF figures attached to pilot/day-rate language
  '\bCHF[[:space:]]+[0-9]'
  '\bday[[:space:]]rate'
  '\bspending[[:space:]]cap'
  # NB: home-LAN IP addresses (e.g. 192.168.x.y) are intentionally NOT
  # in this list. They appear in operational documentation (ADR-045,
  # zbook-runbook, the deployment-runbook) by design, as user-overridable
  # defaults. Adding them here would generate noise without preventing
  # any commercial leak.
)

# ----- Path exceptions -----
# Vendored content + the explicit public-pitch destination + the
# private-data marker file (the gate ITSELF must mention some patterns
# in its own list).
EXCLUDED_PATHS=(
  '^docs/reference/openai/'           # OpenAI's spec — vendored
  '^docs/pitch/'                       # explicitly public pitch destination
  '^docs/phd/'                         # explicitly public phd framing
  '^charts/audittrace/charts/'         # vendored Helm subchart tarballs
  '^scripts/check-no-private-content\.sh$'   # this file itself
  '^\.pre-commit-config\.yaml$'        # config that wires this script in
)

# ----- File-type filter (only text formats this gate cares about) -----
INCLUDED_EXTENSIONS=(
  md py yaml yml sh json toml tpl dsl tf hcl ini env conf
)

# ----- Mode: staged files vs whole repo -----
if [[ "${SCAN_ALL:-0}" == "1" ]]; then
  mapfile -t FILES < <(git ls-files)
  echo "🔎 Scanning the whole repo (SCAN_ALL=1)..."
else
  mapfile -t FILES < <(git diff --cached --name-only --diff-filter=ACMR)
fi

if [[ ${#FILES[@]} -eq 0 ]]; then
  exit 0
fi

# ----- Filter to relevant files -----
RELEVANT=()
for f in "${FILES[@]}"; do
  # Skip non-existent (e.g. deleted) files
  [[ ! -f "$f" ]] && continue
  # Skip excluded paths
  skip=0
  for excl in "${EXCLUDED_PATHS[@]}"; do
    if [[ "$f" =~ $excl ]]; then
      skip=1
      break
    fi
  done
  [[ $skip -eq 1 ]] && continue
  # Keep only included extensions
  ext="${f##*.}"
  match=0
  for inc in "${INCLUDED_EXTENSIONS[@]}"; do
    if [[ "$ext" == "$inc" ]]; then
      match=1
      break
    fi
  done
  [[ $match -eq 1 ]] && RELEVANT+=("$f")
done

if [[ ${#RELEVANT[@]} -eq 0 ]]; then
  exit 0
fi

# ----- Apply forbidden-pattern grep -----
violations=0
for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
  hits=$(grep -niE "$pattern" "${RELEVANT[@]}" 2>/dev/null || true)
  if [[ -n "$hits" ]]; then
    if [[ $violations -eq 0 ]]; then
      echo "❌ Forbidden private/legal content detected in staged files."
      echo ""
      echo "Per feedback_pitch_public_vs_private: customer/counterparty"
      echo "names, contact details, pricing, and internal infra tokens"
      echo "stay in ~/work/audittrace-private/ or ~/work/pitch-private/,"
      echo "NEVER in this repository."
      echo ""
    fi
    echo "Pattern: $pattern"
    echo "$hits"
    echo ""
    violations=$((violations + 1))
  fi
done

if [[ $violations -gt 0 ]]; then
  echo "----"
  echo "Fix options:"
  echo "  1. Move the substance to ~/work/audittrace-private/ or"
  echo "     ~/work/pitch-private/ and reference it generically here."
  echo "  2. If the named party approached publicly (LinkedIn post,"
  echo "     public RFP, etc.) and the file belongs in docs/pitch/ or"
  echo "     docs/phd/, move it there — those subtrees are exempt."
  echo "  3. If the pattern is a genuine false positive (e.g. an"
  echo "     unrelated technical 'flavio' that has nothing to do with"
  echo "     the ContextShield context), open a discussion before"
  echo "     loosening the pattern in this script."
  echo ""
  echo "Do NOT bypass this hook with --no-verify. The check exists"
  echo "because PII / pricing / customer names have leaked twice."
  exit 1
fi

exit 0
