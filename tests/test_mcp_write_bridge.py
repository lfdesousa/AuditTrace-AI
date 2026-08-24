"""Unit tests for ``audittrace.services.mcp_write_bridge`` (ADR-063 Phase
2 Track A), isolated from the real ``write_decision``/``write_skill``
handlers (HTTP-level coverage for those lives in
``test_mcp_write_tools_routes.py``).

Mirrors ``test_mcp_bridge.py``'s stub-registry style so each branch
(disabled tool / unknown tool / missing scope / handler raises / handler
success) is independently provable with a small local registry, rather
than routing every edge through the full FastAPI stack.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from audittrace.identity import UserContext, sentinel_user_context
from audittrace.services.mcp_write_bridge import call_write_tool, list_write_tools
from audittrace.tools.mcp_write_registry import (
    MCP_WRITE_TOOL_REGISTRY,
    register_mcp_write_tool,
    reset_mcp_write_registry_for_tests,
)

_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"document_id": {"type": "string"}},
    "required": ["document_id"],
}


class _CountingHandler:
    def __init__(self, response: dict[str, Any] | None = None):
        self.calls: list[dict[str, Any]] = []
        self._response = response if response is not None else {"ok": True}

    async def __call__(
        self, user_context: UserContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(args)
        return self._response


class _RaisingHandler:
    async def __call__(
        self, user_context: UserContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_mcp_write_registry_for_tests()
    yield
    reset_mcp_write_registry_for_tests()


def _register_write_tool(
    name: str = "write_stub", required_scope: str = "memory:decisions:write"
) -> _CountingHandler:
    handler = _CountingHandler()
    register_mcp_write_tool(
        name=name,
        description="Stub write tool for mcp_write_bridge tests.",
        parameters_schema=_WRITE_SCHEMA,
        required_scope=required_scope,
    )(handler)
    return handler


def _scoped_user(*scopes: str, is_admin: bool = False) -> UserContext:
    return replace(sentinel_user_context(), scopes=scopes, is_admin=is_admin)


class TestListWriteTools:
    def test_disabled_tool_excluded(self) -> None:
        _register_write_tool("write_stub")
        MCP_WRITE_TOOL_REGISTRY["write_stub"] = replace(
            MCP_WRITE_TOOL_REGISTRY["write_stub"], enabled=False
        )
        visible = list_write_tools(_scoped_user("memory:decisions:write"))
        assert visible == []

    def test_scoped_caller_sees_only_own_tool(self) -> None:
        _register_write_tool("write_a", required_scope="memory:decisions:write")
        _register_write_tool("write_b", required_scope="memory:skills:write")
        visible = list_write_tools(_scoped_user("memory:decisions:write"))
        assert [t.name for t in visible] == ["write_a"]

    def test_admin_without_literal_scope_sees_nothing(self) -> None:
        """No admin bypass for write tools (deliberate deviation from
        list_read_tools) — is_admin=True alone must not surface a
        mutation tool."""
        _register_write_tool("write_stub")
        visible = list_write_tools(_scoped_user(is_admin=True))
        assert visible == []


class TestCallWriteTool:
    @pytest.mark.asyncio
    async def test_unknown_tool_denied_no_execution(self) -> None:
        handler = _register_write_tool("write_stub")
        outcome = await call_write_tool(
            user_context=_scoped_user("memory:decisions:write"),
            tool_name="does_not_exist",
            arguments={},
        )
        assert outcome.status == "failed"
        assert outcome.failure_class == "tool_not_found"
        assert handler.calls == []

    @pytest.mark.asyncio
    async def test_missing_scope_denied_no_execution(self) -> None:
        handler = _register_write_tool("write_stub")
        outcome = await call_write_tool(
            user_context=_scoped_user(), tool_name="write_stub", arguments={}
        )
        assert outcome.status == "failed"
        assert outcome.failure_class == "scope_denied"
        assert handler.calls == []

    @pytest.mark.asyncio
    async def test_handler_raises_is_wrapped_as_tool_error(self) -> None:
        register_mcp_write_tool(
            name="write_boom",
            description="Raises unconditionally.",
            parameters_schema=_WRITE_SCHEMA,
            required_scope="memory:decisions:write",
        )(_RaisingHandler())

        outcome = await call_write_tool(
            user_context=_scoped_user("memory:decisions:write"),
            tool_name="write_boom",
            arguments={"document_id": "x"},
        )
        assert outcome.status == "failed"
        assert outcome.failure_class == "tool_error"
        assert outcome.error_detail == "RuntimeError"
        assert outcome.result.isError is True

    @pytest.mark.asyncio
    async def test_handler_success_executes_once(self) -> None:
        handler = _register_write_tool("write_stub")
        outcome = await call_write_tool(
            user_context=_scoped_user("memory:decisions:write"),
            tool_name="write_stub",
            arguments={"document_id": "adr-1"},
        )
        assert outcome.status == "success"
        assert len(handler.calls) == 1
        assert outcome.result.isError is False

    @pytest.mark.asyncio
    async def test_identity_args_stripped_before_dispatch(self) -> None:
        handler = _register_write_tool("write_stub")
        await call_write_tool(
            user_context=_scoped_user("memory:decisions:write"),
            tool_name="write_stub",
            arguments={"document_id": "adr-1", "user_id": "mallory"},
        )
        assert handler.calls == [{"document_id": "adr-1"}]


def test_duplicate_registration_raises() -> None:
    _register_write_tool("write_dup")
    with pytest.raises(ValueError, match="already registered"):
        _register_write_tool("write_dup")


def test_to_mcp_tool_reused_from_phase1_bridge() -> None:
    """``services.mcp_write_bridge`` never redefines ``to_mcp_tool`` —
    ``routes/mcp.py`` reuses ``services.mcp_bridge.to_mcp_tool`` for
    write-tool manifest entries too (one rendering function, not two)."""
    from audittrace.routes.mcp import to_mcp_tool as route_level_to_mcp_tool
    from audittrace.services.mcp_bridge import to_mcp_tool as bridge_to_mcp_tool

    assert route_level_to_mcp_tool is bridge_to_mcp_tool
