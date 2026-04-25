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
# Two sources of forbidden patterns:
#
#   1. **Hardcoded generic patterns** in this script — currency-with-
#      figure shapes, day-rate language, spending-cap language. These
#      are not customer-specific so they live in the public repo.
#
#   2. **Operator-local customer names** in
#      ${AUDITTRACE_PRIVATE_DIR}/forbidden-patterns.txt
#      (default ~/work/audittrace-private/forbidden-patterns.txt).
#      Each line is one regex (POSIX extended, case-insensitive).
#      The file lives OUTSIDE the public repo, so the customer-name
#      list never touches Git history.
#
#      If the file is missing, the gate still runs the hardcoded
#      generic patterns and prints a warning so the operator knows
#      the customer-name layer is not active.
#
# Run automatically as a `pre-commit` hook (see .pre-commit-config.yaml).
# Exit 0 if clean, 1 if any forbidden pattern is found in the staged
# changes.
#
# Usage (manual / debugging):
#     ./scripts/check-no-private-content.sh        # scans staged files
#     SCAN_ALL=1 ./scripts/check-no-private-content.sh   # scans the whole repo

set -euo pipefail

# ----- Hardcoded generic patterns (case-insensitive) -----
# Not customer-specific — safe for the public repo.
GENERIC_PATTERNS=(
  '\bCHF[[:space:]]+[0-9]'
  '\bUSD[[:space:]]+[0-9]'
  '\bEUR[[:space:]]+[0-9]'
  '\bGBP[[:space:]]+[0-9]'
  '\bday[[:space:]]rate'
  '\bspending[[:space:]]cap'
)

# ----- Operator-local customer name list -----
PRIVATE_DIR="${AUDITTRACE_PRIVATE_DIR:-$HOME/work/audittrace-private}"
LOCAL_PATTERN_FILE="${PRIVATE_DIR}/forbidden-patterns.txt"

LOCAL_PATTERNS=()
if [[ -f "${LOCAL_PATTERN_FILE}" ]]; then
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line//[[:space:]]/}" ]] && continue
    LOCAL_PATTERNS+=("$line")
  done < "${LOCAL_PATTERN_FILE}"
fi

# ----- Path exceptions -----
EXCLUDED_PATHS=(
  '^docs/reference/openai/'
  '^docs/pitch/'
  '^docs/phd/'
  '^charts/audittrace/charts/'
  '^scripts/check-no-private-content\.sh$'
  '^\.pre-commit-config\.yaml$'
)

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
  [[ ! -f "$f" ]] && continue
  skip=0
  for excl in "${EXCLUDED_PATHS[@]}"; do
    if [[ "$f" =~ $excl ]]; then
      skip=1
      break
    fi
  done
  [[ $skip -eq 1 ]] && continue
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

# ----- Warn if the operator-local file is missing -----
if [[ ! -f "${LOCAL_PATTERN_FILE}" ]]; then
  echo "⚠  Operator-local pattern file missing: ${LOCAL_PATTERN_FILE}"
  echo "   The customer-name layer of this gate is INACTIVE on this machine."
  echo "   Generic patterns (currency / day-rate / spending-cap) are still"
  echo "   enforced. To activate the customer-name layer, create the file"
  echo "   with one regex per line. Example:"
  echo "       acmecorp"
  echo "       jane[. ]?doe"
  echo "       @acmecorp\\.com"
  echo ""
fi

# ----- Apply forbidden-pattern grep -----
violations=0

apply_pattern() {
  local pattern="$1"
  local hits
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
}

for pattern in "${GENERIC_PATTERNS[@]}"; do
  apply_pattern "$pattern"
done

for pattern in "${LOCAL_PATTERNS[@]}"; do
  apply_pattern "$pattern"
done

if [[ $violations -gt 0 ]]; then
  echo "----"
  echo "Fix options:"
  echo "  1. Move the substance to ~/work/audittrace-private/ or"
  echo "     ~/work/pitch-private/ and reference it generically here."
  echo "  2. If the named party approached publicly (LinkedIn post,"
  echo "     public RFP, etc.) and the file belongs in docs/pitch/ or"
  echo "     docs/phd/, move it there — those subtrees are exempt."
  echo "  3. If the pattern is a genuine false positive, open a"
  echo "     discussion before loosening the pattern. Generic patterns"
  echo "     live in this script; customer-name patterns live in"
  echo "     \${AUDITTRACE_PRIVATE_DIR}/forbidden-patterns.txt."
  echo ""
  echo "Do NOT bypass this hook with --no-verify. The check exists"
  echo "because PII / pricing / customer names have leaked twice."
  exit 1
fi

exit 0
