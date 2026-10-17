"""Black-box tests for scripts/harness/claude-config-guard.py — the tool-agnostic
PreToolUse guard that denies ANY tool from writing .claude/ harness config or
touching token files. Closes the 2026-08-11 Bash-tunnel of a refused Edit.

Each case subprocess-invokes the REAL guard with a synthetic PreToolUse event
(the guard IS the artifact under test), so a regression in the guard's own logic
fails here. `.claude/...` paths are relative so the test is path-portable (the
guard matches the protected suffix anywhere in the string).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = str(
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "harness"
    / "claude-config-guard.py"
)

# (expect_deny, event) — expect_deny=True means the guard must return permissionDecision=deny.
CASES: list[tuple[bool, str, dict]] = [
    (
        True,
        "bash-tunnel python write to .claude/agents (the exact 2026-08-11 vector)",
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": "python3 -c \"open('.claude/agents/audittrace-builder.md','w').write('x')\""
            },
        },
    ),
    (
        True,
        "bash-tunnel echo redirect into .claude/settings.local.json",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "echo '{}' > .claude/settings.local.json"},
        },
    ),
    (
        True,
        "bash-tunnel tee into .claude/hooks",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "echo x | tee .claude/hooks/evil.py"},
        },
    ),
    (
        True,
        "bash-tunnel sed -i on a settings file",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "sed -i s/a/b/ .claude/settings.json"},
        },
    ),
    (
        True,
        "Edit tool -> .claude/settings.local.json",
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/.claude/settings.local.json"},
        },
    ),
    (
        True,
        "Write tool -> .claude/agents/new.md",
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "/repo/.claude/agents/new.md"},
        },
    ),
    (
        True,
        "Bash read of a token file",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "cat ~/.config/audittrace/" + "tokens.json"},
        },
    ),
    (
        True,
        "Edit tool -> a token file",
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/home/x/.config/audittrace/" + "tokens.json"},
        },
    ),
    (
        False,
        "benign: git worktree list",
        {"tool_name": "Bash", "tool_input": {"command": "git worktree list"}},
    ),
    (
        False,
        "carve-out: rm a .claude/worktrees dir (operational, not config)",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf .claude/worktrees/agent-x"},
        },
    ),
    (
        False,
        "Read tool on an agent def (reads allowed)",
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "/repo/.claude/agents/audittrace-builder.md"},
        },
    ),
    (
        False,
        "read-only Bash mentioning .claude/settings with '->' string (must NOT trip on the >)",
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": "python3 -c \"print('a -> b', open('.claude/settings.local.json').read())\""
            },
        },
    ),
    (
        False,
        "read-only python grep over .claude/agents (TOKEN-GUARD scanner must NOT be denied)",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "python3 scripts/check-agent-def-token-guard.py"},
        },
    ),
]


def _decision(event: dict) -> str:
    r = subprocess.run(
        [sys.executable, GUARD],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if not r.stdout.strip():
        return "allow"
    try:
        return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"]
    except (KeyError, json.JSONDecodeError):
        return "allow"


@pytest.mark.parametrize("expect_deny,desc,event", CASES, ids=[c[1] for c in CASES])
def test_claude_config_guard(expect_deny: bool, desc: str, event: dict) -> None:
    decision = _decision(event)
    if expect_deny:
        assert decision == "deny", f"guard should DENY: {desc}"
    else:
        assert decision != "deny", f"guard should ALLOW: {desc}"


def test_guard_is_executable_and_present() -> None:
    p = Path(GUARD)
    assert p.exists(), "claude-config-guard.py must exist"
    assert p.stat().st_mode & 0o111, "guard must be executable (chmod +x)"
