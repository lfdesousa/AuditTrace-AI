"""SOFTDELETE-DIRECT-READ: the Bruno requests the live evidence plan runs.

A request that names a variable no environment defines fails at run time, on
the cluster, in front of the operator. These pins fail it here: every variable
the changed requests use is defined in BOTH environments with the default the
request needs, and the two session-pinned MCP requests send the header.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

COLLECTION = Path(__file__).resolve().parents[1] / "bruno" / "audittrace"
REQUESTS = [
    "context/01-context.bru",
    "mcp/02-tools-call-read.bru",
    "mcp/02b-tools-call-read-document.bru",
    "memory/episodic/03-read.bru",
    "memory/procedural/03-read.bru",
    "memory/semantic/03-read.bru",
    "memory/semantic/03b-read-whole-document.bru",
    "memory/semantic/05-delete.bru",
    "memory/index-single-file-md-to-decisions.bru",
]
DEFAULTS = {
    "expectedReadStatus": "200",
    "expectedIndexStatus": "200",
    "deleteHard": "false",
    "mcpRecallTool": "recall_semantic",
    "mcpReadTool": "read_decision",
}
RUNTIME_VARS = {"mcpSessionId"}


def _env(name: str) -> dict[str, str]:
    text = (COLLECTION / "environments" / f"{name}.bru").read_text()
    return dict(re.findall(r"^  (\w+): (.*)$", text, re.M))


@pytest.mark.parametrize("env", ["cluster", "local"])
@pytest.mark.parametrize("request_path", REQUESTS)
def test_every_variable_of_the_request_is_defined(request_path: str, env: str) -> None:
    used = set(re.findall(r"\{\{(\w+)\}\}", (COLLECTION / request_path).read_text()))
    # accessToken is the collection bridge's, baseUrl and the example keys are env vars
    missing = used - set(_env(env)) - RUNTIME_VARS - {"accessToken"}
    assert not missing, f"{request_path}: undefined in {env}: {sorted(missing)}"


@pytest.mark.parametrize("env", ["cluster", "local"])
@pytest.mark.parametrize(("name", "value"), sorted(DEFAULTS.items()))
def test_the_default_keeps_the_request_what_it_was(
    env: str, name: str, value: str
) -> None:
    assert _env(env)[name] == value


@pytest.mark.parametrize(
    "request_path",
    ["mcp/02-tools-call-read.bru", "mcp/02b-tools-call-read-document.bru"],
)
def test_the_mcp_reads_pin_the_session(request_path: str) -> None:
    text = (COLLECTION / request_path).read_text()
    assert "X-Session-Id: {{mcpSessionId}}" in text
    assert 'bru.setVar(\n      "mcpSessionId"' in text


def test_the_read_requests_assert_the_expected_status_not_a_hardcoded_200() -> None:
    for request_path in REQUESTS[3:7]:
        text = (COLLECTION / request_path).read_text()
        assert 'Number(bru.getEnvVar("expectedReadStatus"))' in text, request_path
        assert "expect(res.getStatus()).to.equal(200)" not in text, request_path
