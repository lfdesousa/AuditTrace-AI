"""Guard for the autouse ``_restore_tool_registries`` fixture (tests/conftest.py).

Why: ``MEMORY_TOOL_REGISTRY`` / ``_BY_VISIBLE_NAME`` / ``MCP_WRITE_TOOL_REGISTRY``
are process-global. Many fixtures empty them with ``reset_registry_for_tests()``
and only re-empty on teardown, so a later test on the same process saw no memory
tools (PR #468 CI run 44540571356, worker gw0). Falsifiability: delete the
autouse fixture and ``test_next_test_sees_the_registries_the_run_started_with``
FAILS — the first cell empties the registries, the second finds them empty.

The two cells are ORDERED, so they share one xdist group (same worker, file
order under ``--dist loadgroup``); serially pytest keeps definition order.
"""

from __future__ import annotations

import pytest

from audittrace import tools as tools_mod
from audittrace.tools.mcp_write_registry import (
    MCP_WRITE_TOOL_REGISTRY,
    reset_mcp_write_registry_for_tests,
)

# Import-time state: the conftest imports the app (and so every handler module)
# before this file is collected.
_BASELINE = (
    set(tools_mod.MEMORY_TOOL_REGISTRY),
    dict(tools_mod._BY_VISIBLE_NAME),
    set(MCP_WRITE_TOOL_REGISTRY),
)

pytestmark = pytest.mark.xdist_group("tool-registry-isolation")


def test_baseline_is_populated() -> None:
    """Without this the pair below could pass vacuously on empty registries."""
    memory, aliases, write = _BASELINE
    assert len(memory) >= 7, sorted(memory)
    assert aliases
    assert write


def test_a_leaves_the_registries_empty_like_the_resetter_fixtures_do() -> None:
    tools_mod.reset_registry_for_tests()
    reset_mcp_write_registry_for_tests()
    assert not tools_mod.MEMORY_TOOL_REGISTRY
    assert not MCP_WRITE_TOOL_REGISTRY


def test_next_test_sees_the_registries_the_run_started_with() -> None:
    memory, aliases, write = _BASELINE
    assert set(tools_mod.MEMORY_TOOL_REGISTRY) == memory
    assert dict(tools_mod._BY_VISIBLE_NAME) == aliases
    assert set(MCP_WRITE_TOOL_REGISTRY) == write
