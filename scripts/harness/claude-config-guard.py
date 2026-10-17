#!/usr/bin/env python3
"""PreToolUse guard: deny ANY tool (Bash / Write / Edit / NotebookEdit) from
writing the Claude harness config or touching token files.

Closes the denial-tunnel a subagent used on 2026-08-11: an ``Edit`` to
``.claude/settings.local.json`` was refused, and the agent routed the write
through ``Bash`` (python file-write) instead. A deny that only covers the
Read/Edit tools is porous — exactly the TOKEN-GUARD thesis. This guard is
tool-agnostic and fail-closed.

Register in settings.json:
  {"hooks": {"PreToolUse": [{"matcher": "Bash|Write|Edit|NotebookEdit",
     "hooks": [{"type": "command",
        "command": "/home/lfdesousa/work/AuditTrace-AI/scripts/harness/claude-config-guard.py"}]}]}}

Protected (write-denied via any tool, read still allowed via the Read tool):
  .claude/settings*.json  .claude/agents/**  .claude/hooks/**
Token files (any Bash access denied — the Read tool already denies these):
  **/tokens.json  ~/.config/audittrace/**
Explicitly ALLOWED (operational, not harness config):  .claude/worktrees/**
"""

from __future__ import annotations

import json
import re
import sys

# Harness-config paths that no tool may WRITE (agents read via the Read tool).
_PROTECTED_CONFIG = re.compile(r"\.claude/(settings[^/]*\.json|agents/|hooks/)")
# Token material — no Bash access at all (Read tool already denies these).
_TOKEN_PATHS = re.compile(r"(tokens\.json|\.config/audittrace/)")
# Operational carve-out: worktrees are not harness config.
_ALLOW = re.compile(r"\.claude/worktrees/")
# Bash write indicators (redirect / copy / in-place edit / python write).
# The redirect ``>``/``>>`` must be a real redirect, not a ``>`` inside a
# string literal like ``'->'`` (negative lookbehind excludes word-char / ``>`` /
# ``-``), so read-only commands that merely mention a protected path are allowed
# (e.g. the TOKEN-GUARD scanner reading ``.claude/agents``).
_WRITE_VERB = re.compile(
    r"((?<![\w>-])>>?|\btee\b|\bcp\b|\bmv\b|\bdd\b|\btruncate\b|\binstall\b|"
    r"sed\s+-i|open\([^)]*['\"][wax]|write_text|writelines|\bshutil\b)"
)


def _deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        # Fail-closed only on parseable events; an unreadable event is not a write.
        return
    tool = event.get("tool_name", "")
    ti = event.get("tool_input", {}) or {}

    if tool in ("Write", "Edit", "NotebookEdit"):
        path = str(ti.get("file_path") or ti.get("notebook_path") or "")
        if _ALLOW.search(path):
            return
        if _PROTECTED_CONFIG.search(path) or _TOKEN_PATHS.search(path):
            _deny(
                f"{tool} to harness config / token path is denied "
                f"(claude-config-guard): {path}. Edit .claude/ files "
                f"directly in your editor — the AI does not write its own guardrails."
            )
        return

    if tool == "Bash":
        cmd = str(ti.get("command") or "")
        # Token files: any Bash access denied (read or write).
        if _TOKEN_PATHS.search(cmd):
            _deny("Bash access to a token file is denied (claude-config-guard).")
        # Harness config: deny when a protected path appears with a write verb.
        # Ignore the worktrees carve-out.
        scan = _ALLOW.sub("", cmd)
        if _PROTECTED_CONFIG.search(scan) and _WRITE_VERB.search(scan):
            _deny(
                "Bash write to .claude/ harness config is denied "
                "(claude-config-guard) — the exact tunnel closed 2026-08-11. "
                "Harness config is human-edited only."
            )
        return


if __name__ == "__main__":
    main()
