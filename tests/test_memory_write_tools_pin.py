"""MEMORY-SERVER-SILENT-TRUNCATION ADDENDUM-1, WU-2 (gate r1 S7): the MCP write
tools are pinned like the write routes. A new write tool reddens this until it
is given an over-limit case in ``tests/test_memory_write_limit.py``."""

from __future__ import annotations

import inspect

from audittrace.tools import mcp_write_handlers
from audittrace.tools.mcp_write_registry import MCP_WRITE_TOOL_REGISTRY
from tests import test_memory_write_limit

# The tools ``test_mcp_write_tool_over_the_limit_is_a_tool_error_and_not_indexed``
# is parametrized over.
BOUNDED_TOOLS = {"write_decision", "write_skill"}


class TestMemoryWriteToolsPin:
    def test_every_registered_mcp_write_tool_has_an_over_limit_case(self) -> None:
        assert mcp_write_handlers is not None  # importing it registers the tools
        assert set(MCP_WRITE_TOOL_REGISTRY) == BOUNDED_TOOLS

    def test_the_pinned_names_are_the_ones_the_limit_test_exercises(self) -> None:
        source = inspect.getsource(
            test_memory_write_limit.TestMemoryWriteLimit.test_mcp_write_tool_over_the_limit_is_a_tool_error_and_not_indexed
        )
        marks = [
            mark
            for mark in test_memory_write_limit.TestMemoryWriteLimit.test_mcp_write_tool_over_the_limit_is_a_tool_error_and_not_indexed.pytestmark
            if mark.name == "parametrize"
        ]
        assert source
        assert {case[0] for case in marks[0].args[1]} == BOUNDED_TOOLS
