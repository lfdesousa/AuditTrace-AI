"""The MCP-only write/curation tool registry (ADR-063 Phase 2 Track A).

Deliberately a SEPARATE module from ``tools/mcp_write_handlers.py`` (the
registrant module that defines ``write_decision``/``write_skill`` and
decorates them into this dict) — mirrors the split between
``audittrace.tools`` (the ``MEMORY_TOOL_REGISTRY`` dict, never reloaded)
and ``audittrace.tools.memory_handlers`` (the registrant module tests
safely ``importlib.reload`` between runs). If the dict and the
decorators that populate it lived in the same module, reloading that
module for test isolation would rebind the module attribute to a BRAND
NEW dict object — any OTHER module that already did
``from ... import MCP_WRITE_TOOL_REGISTRY`` (a name-binding import, not
a live reference into the module) would keep pointing at the stale,
now-orphaned dict. Keeping the registry in its own never-reloaded module
is what makes ``services.mcp_write_bridge``'s import stay valid across
``tests/test_mcp_write_tools_routes.py``'s per-test
``reset_mcp_write_registry_for_tests()`` + ``importlib.reload(
tools.mcp_write_handlers)`` cycle.

Deliberately NOT ``audittrace.tools.MEMORY_TOOL_REGISTRY`` — see
``tools/mcp_write_handlers.py``'s module docstring for why write/
curation tools must stay structurally invisible to
``audittrace.tools.tools_visible_to`` (the function
``routes/chat.py``'s tool-call loop reads) and to
``services.mcp_bridge`` (Phase 1's read-tool dispatch).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from audittrace.tools import MemoryTool, ToolHandler

logger = logging.getLogger(__name__)

MCP_WRITE_TOOL_REGISTRY: dict[str, MemoryTool] = {}


def register_mcp_write_tool(
    *,
    name: str,
    description: str,
    parameters_schema: dict[str, Any],
    required_scope: str,
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator registering an MCP-only write tool at import time.
    Mirrors ``audittrace.tools.register_memory_tool``'s shape."""

    def _decorate(handler: ToolHandler) -> ToolHandler:
        if name in MCP_WRITE_TOOL_REGISTRY:
            raise ValueError(f"MCP write tool {name!r} is already registered")
        MCP_WRITE_TOOL_REGISTRY[name] = MemoryTool(
            name=name,
            description=description,
            parameters_schema=parameters_schema,
            required_scope=required_scope,
            handler=handler,
            enabled=True,
        )
        logger.debug("Registered MCP write tool %r (scope=%s)", name, required_scope)
        return handler

    return _decorate


def reset_mcp_write_registry_for_tests() -> None:
    """Drop every registered MCP write tool. Test-fixture support only —
    mirrors ``audittrace.tools.reset_registry_for_tests``. Clears the
    dict IN PLACE (never rebinds the module attribute to a new object)
    so every other module's ``from ... import MCP_WRITE_TOOL_REGISTRY``
    stays valid."""
    MCP_WRITE_TOOL_REGISTRY.clear()
