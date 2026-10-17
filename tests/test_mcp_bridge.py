"""Tests for ``audittrace.services.mcp_bridge`` (ADR-063 Phase 1).

The bridge composes three EXISTING seams (the memory-tool registry, the
cache-aware ``invoke_tool`` dispatcher, and the ``PendingToolCall`` audit
shape) rather than reimplementing authorization/execution/auditing —
these tests exercise the bridge's own composition logic in isolation
from the real handlers (which have their own extensive test coverage in
``test_memory_tool_handlers.py``), using small registry-local stub
tools so each branch (unknown tool / write-scope boundary / missing
scope / handler error / handler success) is independently provable.

Falsifiable-by-construction: every "denied" test asserts the stub
handler's call-counter stayed at zero. Removing the corresponding guard
in ``mcp_bridge.py`` would let the handler run, flipping the counter to
1 and turning the assertion RED — this is what makes these non-vacuous
per ``feedback_vacuous_neuter_test_antipattern``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from audittrace.identity import UserContext, sentinel_user_context
from audittrace.services.mcp_bridge import (
    _is_read_scope,
    _strip_identity_args,
    call_read_tool,
    list_read_tools,
    to_mcp_tool,
)
from audittrace.tools import (
    MEMORY_TOOL_REGISTRY,
    register_memory_tool,
    reset_registry_for_tests,
)

_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}


class _CountingHandler:
    """Async handler stub that records every invocation's args."""

    def __init__(self, response: dict[str, Any] | None = None):
        self.calls: list[dict[str, Any]] = []
        self._response = response if response is not None else {"ok": True}

    async def __call__(
        self, user_context: UserContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(args)
        return self._response


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test in this file builds its own tiny registry — real
    ``memory_handlers`` tools are irrelevant to the bridge's own
    composition logic (and this keeps the tests hermetic + fast)."""
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


def _register_read_tool(
    name: str = "recall_stub", response: dict[str, Any] | None = None
) -> _CountingHandler:
    handler = _CountingHandler(response)
    register_memory_tool(
        name=name,
        description="Stub read tool for mcp_bridge tests.",
        parameters_schema=_READ_SCHEMA,
        required_scope="memory:episodic:read",
    )(handler)
    return handler


def _register_write_tool(
    name: str = "write_stub", response: dict[str, Any] | None = None
) -> _CountingHandler:
    handler = _CountingHandler(response)
    register_memory_tool(
        name=name,
        description="Stub write tool — must NEVER reach the MCP surface.",
        parameters_schema=_READ_SCHEMA,
        required_scope="memory:episodic:write",
    )(handler)
    return handler


# ───────────────────────────── _is_read_scope ───────────────────────────────


class TestIsReadScope:
    @pytest.mark.parametrize(
        "scope,expected",
        [
            ("memory:episodic:read", True),
            ("memory:procedural:read", True),
            ("memory:conversational:read-own", True),
            ("memory:corpus:decisions:read", True),
            ("memory:episodic:write", False),
            ("memory:corpus:decisions:write", False),
            ("audittrace:admin", False),
            ("memory:upload:write", False),
            ("", False),
        ],
    )
    def test_read_scope_classification(self, scope: str, expected: bool) -> None:
        assert _is_read_scope(scope) is expected


# ─────────────────────────── list_read_tools ────────────────────────────────


class TestListReadTools:
    def test_manifest_excludes_write_tools_for_admin(self) -> None:
        """Falsifiable acceptance #1: 'leak a write tool into the
        manifest' must stay RED. An admin — who bypasses every OTHER
        scope check in this codebase — must still never see a write
        tool through MCP Phase 1."""
        _register_read_tool("recall_stub")
        _register_write_tool("write_stub")

        admin = sentinel_user_context()
        assert admin.is_admin is True

        names = {t.name for t in list_read_tools(admin)}
        assert names == {"recall_stub"}
        assert "write_stub" not in names

    def test_manifest_filters_by_caller_scope_for_non_admin(self) -> None:
        _register_read_tool("recall_stub")
        other_read = replace(
            sentinel_user_context(),
            user_id="u1",
            is_admin=False,
            scopes=(),
        )
        assert list_read_tools(other_read) == []

        with_scope = replace(other_read, scopes=("memory:episodic:read",))
        names = {t.name for t in list_read_tools(with_scope)}
        assert names == {"recall_stub"}

    def test_manifest_excludes_disabled_tools(self) -> None:
        from dataclasses import replace as dc_replace

        _register_read_tool("recall_stub")
        MEMORY_TOOL_REGISTRY["recall_stub"] = dc_replace(
            MEMORY_TOOL_REGISTRY["recall_stub"], enabled=False
        )
        assert list_read_tools(sentinel_user_context()) == []

    def test_to_mcp_tool_shape(self) -> None:
        _register_read_tool("recall_stub")
        tool = MEMORY_TOOL_REGISTRY["recall_stub"]
        mcp_tool = to_mcp_tool(tool)
        assert mcp_tool.name == "recall_stub"
        assert mcp_tool.description == tool.description
        assert mcp_tool.inputSchema == _READ_SCHEMA


# ─────────────────────────── _strip_identity_args ───────────────────────────


class TestStripIdentityArgs:
    @pytest.mark.parametrize("key", ["user_id", "sub", "userId"])
    def test_strips_identity_shaped_keys(self, key: str) -> None:
        out = _strip_identity_args({"query": "x", key: "attacker-controlled"})
        assert out == {"query": "x"}

    def test_preserves_other_keys(self) -> None:
        out = _strip_identity_args({"query": "x", "n": 5, "offset": 0})
        assert out == {"query": "x", "n": 5, "offset": 0}


# ─────────────────────────── call_read_tool ─────────────────────────────────


class TestCallReadToolDenied:
    @pytest.mark.asyncio
    async def test_unknown_tool_denied(self) -> None:
        user = sentinel_user_context()
        outcome = await call_read_tool(
            user_context=user,
            session_id="sess-1",
            tool_name="does_not_exist",
            arguments={},
        )
        assert outcome.status == "failed"
        assert outcome.failure_class == "tool_not_found"
        assert outcome.result.isError is True
        assert outcome.pending.error is not None
        assert outcome.pending.granted_scope == ""

    @pytest.mark.asyncio
    async def test_write_tool_denied_even_for_admin(self) -> None:
        """Defense-in-depth: even though ``list_read_tools`` never
        advertises a write tool, a client can call ``tools/call`` with
        any name. Neuter (remove the ``_is_read_scope`` re-check inside
        ``call_read_tool``) → this test goes RED because the admin's
        scope bypass would let the write handler execute."""
        handler = _register_write_tool("write_stub")
        admin = sentinel_user_context()
        assert admin.is_admin is True

        outcome = await call_read_tool(
            user_context=admin,
            session_id="sess-1",
            tool_name="write_stub",
            arguments={"query": "x"},
        )
        assert outcome.status == "failed"
        assert outcome.failure_class == "phase1_write_boundary"
        assert outcome.result.isError is True
        assert handler.calls == [], "write handler must never execute via MCP Phase 1"

    @pytest.mark.asyncio
    async def test_missing_scope_denied_no_execution_no_data(self) -> None:
        """Falsifiable acceptance #3: missing-scope call is denied — no
        execution, no data. Neuter (drop the scope check) → RED because
        the handler's call-counter would flip to 1 and structuredContent
        would carry the handler's payload."""
        handler = _register_read_tool("recall_stub", response={"secret": "leak"})
        caller = replace(
            sentinel_user_context(),
            user_id="u-no-scope",
            is_admin=False,
            scopes=(),
        )

        outcome = await call_read_tool(
            user_context=caller,
            session_id="sess-1",
            tool_name="recall_stub",
            arguments={"query": "x"},
        )
        assert outcome.status == "failed"
        assert outcome.failure_class == "scope_denied"
        assert outcome.result.isError is True
        assert outcome.result.structuredContent is None
        assert handler.calls == [], "handler must never execute on scope denial"
        assert outcome.pending.granted_scope == "memory:episodic:read"

    @pytest.mark.asyncio
    async def test_handler_error_result_marks_failed(self) -> None:
        handler = _register_read_tool("recall_stub", response={"error": "boom"})
        admin = sentinel_user_context()
        outcome = await call_read_tool(
            user_context=admin,
            session_id="sess-1",
            tool_name="recall_stub",
            arguments={"query": "x"},
        )
        assert handler.calls, "handler SHOULD run when scope is granted"
        assert outcome.status == "failed"
        assert outcome.failure_class == "tool_error"
        assert outcome.result.isError is True


class TestCallReadToolSuccess:
    @pytest.mark.asyncio
    async def test_valid_scope_executes_and_returns_data(self) -> None:
        """Falsifiable acceptance #2 (bridge layer): a valid-scope call
        executes and returns a result AND produces exactly one
        PendingToolCall carrying the token-derived user_id."""
        handler = _register_read_tool(
            "recall_stub", response={"matches": [{"id": "abc"}], "total": 1}
        )
        caller = replace(
            sentinel_user_context(),
            user_id="u-alice",
            is_admin=False,
            scopes=("memory:episodic:read",),
        )

        outcome = await call_read_tool(
            user_context=caller,
            session_id="sess-1",
            tool_name="recall_stub",
            arguments={"query": "x"},
        )

        assert len(handler.calls) == 1
        assert outcome.status == "success"
        assert outcome.failure_class is None
        assert outcome.result.isError is False
        assert outcome.result.structuredContent == {
            "matches": [{"id": "abc"}],
            "total": 1,
        }
        assert outcome.pending.user_id == "u-alice"
        assert outcome.pending.granted_scope == "memory:episodic:read"
        assert outcome.pending.error is None
        assert outcome.pending.result_summary is not None

    @pytest.mark.asyncio
    async def test_caller_supplied_identity_arg_is_stripped_before_dispatch(
        self,
    ) -> None:
        """Falsifiable acceptance #5: a caller-supplied user_id in the
        payload is IGNORED. Neuter (stop stripping identity keys) → RED
        because the handler would receive 'user_id' in its args dict."""
        handler = _register_read_tool("recall_stub")
        admin = sentinel_user_context()

        await call_read_tool(
            user_context=admin,
            session_id="sess-1",
            tool_name="recall_stub",
            arguments={"query": "x", "user_id": "attacker-controlled-id"},
        )

        assert len(handler.calls) == 1
        assert "user_id" not in handler.calls[0]
        assert handler.calls[0] == {"query": "x"}

    @pytest.mark.asyncio
    async def test_admin_bypasses_scope_check_for_read_tools(self) -> None:
        """Admins bypass the per-tool scope gate (matching
        ``tools_visible_to``'s existing admin semantics) for READ tools
        specifically — the write-boundary test above proves that bypass
        stops at the Phase 1 read/write line."""
        _register_read_tool("recall_stub")
        admin = replace(sentinel_user_context(), scopes=(), is_admin=True)
        outcome = await call_read_tool(
            user_context=admin,
            session_id="sess-1",
            tool_name="recall_stub",
            arguments={"query": "x"},
        )
        assert outcome.status == "success"
