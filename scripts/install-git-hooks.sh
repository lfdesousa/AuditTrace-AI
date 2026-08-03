#!/usr/bin/env bash
# Installs the tracked pre-push hook (`.githooks/pre-push`, #397, marker
# ADR049-PRBODY-AUTOENFORCE-20260803) into this repo's git hooks
# directory. Invoked by `make install-hooks` (also wired into
# `make install`).
#
# Deliberately does NOT set `core.hooksPath`: pre-commit 4.x's own
# `pre-commit install` — which manages the `pre-commit` and `commit-msg`
# stage hooks per `.pre-commit-config.yaml` and already runs at the end
# of `make install` — hard-refuses to run whenever `core.hooksPath` is
# set:
#
#   [ERROR] Cowardly refusing to install hooks with `core.hooksPath` set.
#
# (verified empirically 2026-08-03 while building #397, against
# pre-commit 4.6.1). Repointing hooksPath would therefore CLOBBER the
# ruff/mypy/semgrep/commit-evidence/requirements-sync hooks `make
# install` sets up — exactly what the spec forbids. Installing directly
# into the default hooks directory (`git rev-parse --git-path hooks`,
# which resolves correctly for worktrees too), in the ONE hook slot
# (`pre-push`) `.pre-commit-config.yaml` does not manage, composes
# cleanly with zero risk of touching those other hooks.
#
# Idempotent: re-running when `.githooks/pre-push` hasn't changed is a
# silent no-op copy (same bytes in, same bytes out). If a DIFFERENT
# pre-push hook already exists (e.g. the historical untracked,
# gitleaks-only hook this replaces — see
# `project_gitleaks_hooks_portable`), it is backed up once with a
# timestamp suffix before being overwritten; the tracked hook already
# re-invokes gitleaks itself, so no scanning protection is lost
# (composed, not clobbered).

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOKS_DIR="$(git rev-parse --git-path hooks)"
SRC="$REPO_ROOT/.githooks/pre-push"
DEST="$HOOKS_DIR/pre-push"

mkdir -p "$HOOKS_DIR"

if [ -f "$DEST" ] && ! cmp -s "$SRC" "$DEST"; then
    backup="${DEST}.pre-397-backup.$(date +%Y%m%d%H%M%S)"
    echo "ℹ️  Existing $DEST differs from the tracked hook — backing up to $backup"
    cp "$DEST" "$backup"
fi

install -m 0755 "$SRC" "$DEST"
echo "✅ pre-push hook installed: $DEST (source: .githooks/pre-push)"
