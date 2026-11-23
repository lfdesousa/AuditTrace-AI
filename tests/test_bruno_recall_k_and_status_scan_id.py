"""Pin the two Bruno request fixes (recall ``k`` variable, status scan_id test).

* ``mcp/02-tools-call-read.bru`` sends ``k`` from ``{{mcpRecallK}}`` (not a literal),
  and both environments default it to 3, the value the request always sent.
* ``memory/upload-status-clean.bru`` compares the echoed scan_id with the runtime
  var, else the env var, so a standalone ``--env-var cleanScanId=<id>`` run passes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

COLLECTION = Path(__file__).resolve().parents[1] / "bruno" / "audittrace"
MCP_READ = COLLECTION / "mcp" / "02-tools-call-read.bru"
STATUS = COLLECTION / "memory" / "upload-status-clean.bru"


def test_mcp_read_sends_k_from_the_variable() -> None:
    text = MCP_READ.read_text()
    assert '"k": {{mcpRecallK}}' in text
    assert not re.search(r'"k":\s*\d', text)


@pytest.mark.parametrize("env", ["cluster", "local"])
def test_both_environments_default_k_to_three(env: str) -> None:
    text = (COLLECTION / "environments" / f"{env}.bru").read_text()
    assert re.findall(r"^  mcpRecallK: (\S+)$", text, re.M) == ["3"]


def test_status_scan_id_test_reads_runtime_var_or_env_var() -> None:
    text = STATUS.read_text()
    assert 'to.equal(bru.getVar("cleanScanId") || bru.getEnvVar("cleanScanId"))' in text
