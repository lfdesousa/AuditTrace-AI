"""LC-10a AC-17: every session-writing / session-recalling Bruno request sends
``X-Session-Id`` from ``chatSessionId`` with the run-shared pre-request script.

The requests are PARSED (blocks, not substrings), so a deleted header line, a
header that moved out of the ``headers`` block, a renamed variable or a lost
pre-request script each turn a test RED.

Neuters: delete the header line from one request -> that request's cell RED;
rename the variable -> RED; drop the script -> RED; drop an env var the
folder uses from ``cluster-session-e2e.bru`` -> the env cell RED.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

COLLECTION = Path(__file__).resolve().parents[1] / "bruno" / "audittrace"
FOLDER = COLLECTION / "session-recall"
REQUESTS = sorted(FOLDER.glob("*.bru")) + [
    COLLECTION / "mcp" / "03-tools-call-write.bru",
    COLLECTION / "mcp" / "04-tools-call-broker-template.bru",
    COLLECTION / "memory" / "upload-legacy.bru",
]
ENV = COLLECTION / "environments" / "cluster-session-e2e.bru"
EXPECTED_FOLDER = [
    "01-chat-recall-expect-empty.bru",
    "02-upload-session-note.bru",
    "03-chat-recall-stream-false.bru",
    "04-chat-recall-stream-true.bru",
    "05-mcp-tools-call-recall.bru",
    "06-audit-interactions-by-session.bru",
]


def _block(text: str, name: str) -> str | None:
    m = re.search(rf"^{re.escape(name)} \{{\n(.*?)^\}}\n", text, re.S | re.M)
    return m.group(1) if m else None


def test_the_folder_holds_exactly_the_six_requests_in_order() -> None:
    assert [p.name for p in sorted(FOLDER.glob("*.bru"))] == EXPECTED_FOLDER
    seqs = [
        int(re.search(r"seq: (\d+)", p.read_text()).group(1))  # type: ignore[union-attr]
        for p in sorted(FOLDER.glob("*.bru"))
    ]
    assert seqs == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize("path", REQUESTS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_session_requests_carry_x_session_id(path: Path) -> None:
    text = path.read_text()
    headers = _block(text, "headers")
    assert headers is not None, f"{path.name}: no headers block"
    lines = [ln.strip() for ln in headers.splitlines() if ln.strip()]
    assert "X-Session-Id: {{chatSessionId}}" in lines, path.name
    script = _block(text, "script:pre-request")
    assert script is not None, f"{path.name}: no pre-request script"
    assert 'bru.setVar(\n      "chatSessionId"' in script, path.name
    assert 'bru.getEnvVar("chatSessionId")' in script, path.name


def test_the_session_upload_targets_the_session_layer_with_the_note_file() -> None:
    text = (FOLDER / "02-upload-session-note.bru").read_text()
    assert "/memory/upload?layer=session&filename={{sessionNoteName}}" in text
    assert "@file({{uploadSessionFile}})" in text


def test_every_var_the_folder_uses_is_defined_in_the_session_env() -> None:
    env = ENV.read_text()
    defined = set(re.findall(r"^  (\w+):", _block(env, "vars") or "", re.M))
    runtime = {"chatSessionId"}  # generated per run; the env may leave it empty
    used: set[str] = set()
    for p in FOLDER.glob("*.bru"):
        used |= set(re.findall(r"\{\{(\w+)\}\}", p.read_text()))
    secrets = {"accessToken"}
    assert used - secrets - runtime <= defined, sorted(
        used - secrets - runtime - defined
    )


def test_the_session_env_uses_the_dedicated_client_and_both_scopes_by_name() -> None:
    vars_ = _block(ENV.read_text(), "vars") or ""
    assert "clientId: audittrace-session-e2e" in vars_
    scope = re.search(r"^  scope: (.*)$", vars_, re.M)
    assert scope is not None
    assert set(scope.group(1).split()) == {
        "audittrace:query",
        "audittrace:audit",
        "memory:session:write",
        "memory:session:read-own",
    }
