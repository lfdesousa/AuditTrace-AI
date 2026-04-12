#!/usr/bin/env bash
# Bulk-create GitHub issues from the markdown files in this directory.
#
# Each NN-*.md file is parsed for YAML front matter (title, labels, priority)
# and the body is posted as the issue body via `gh issue create`.
#
# Requirements:
#   - gh CLI installed and authenticated (`gh auth status`)
#   - Run from any directory; the script resolves its own path
#
# Usage:
#   ./create-issues.sh           # creates all issues that don't already exist
#   ./create-issues.sh --dry-run # parse + print without calling gh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=1
fi

if [ "$DRY_RUN" -eq 0 ]; then
    if ! command -v gh >/dev/null 2>&1; then
        echo "Error: gh CLI not installed. Install with: sudo apt install gh" >&2
        exit 1
    fi
    if ! gh auth status >/dev/null 2>&1; then
        echo "Error: gh not authenticated. Run: gh auth login" >&2
        exit 1
    fi
fi

# Get the list of existing open issue titles so we don't double-create
existing=""
if [ "$DRY_RUN" -eq 0 ]; then
    existing="$(gh issue list --state all --limit 200 --json title --jq '.[].title')"
fi

shopt -s nullglob
for f in "$DIR"/[0-9][0-9]-*.md; do
    name="$(basename "$f")"

    # Extract YAML front matter (between the first two `---` markers)
    title="$(awk '/^---$/{n++; next} n==1 && /^title:/ {sub(/^title: */, ""); gsub(/^"|"$/, ""); print; exit}' "$f")"
    labels="$(awk '/^---$/{n++; next} n==1 && /^labels:/ {sub(/^labels: */, ""); print; exit}' "$f" | tr -d '[]"' | tr ',' '\n' | sed 's/^ *//;s/ *$//' | paste -sd, -)"

    if [ -z "$title" ]; then
        echo "Skip $name: missing title in front matter" >&2
        continue
    fi

    body="$(awk '/^---$/{n++; next} n>=2' "$f")"

    if [ "$DRY_RUN" -eq 1 ]; then
        echo "── $name ────────────────────────────────────"
        echo "title:  $title"
        echo "labels: $labels"
        echo "body length: ${#body} bytes"
        echo
        continue
    fi

    if echo "$existing" | grep -Fxq "$title"; then
        echo "Skip $name: issue already exists with same title"
        continue
    fi

    echo "Creating: $title"
    if [ -n "$labels" ]; then
        gh issue create --title "$title" --body "$body" --label "$labels"
    else
        gh issue create --title "$title" --body "$body"
    fi
done
