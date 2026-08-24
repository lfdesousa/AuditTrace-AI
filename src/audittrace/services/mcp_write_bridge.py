"""Bridge: MCP ``tools/call`` → the MCP-only write/curation tool registry
+ the SAME tamper-evident audit path (ADR-063 Phase 2 Track A).

Mirrors ``services.mcp_bridge``'s authorize → execute → record → return
shape for the read tools, with two deliberate differences documented
below (per-tool-only scope, no cache).

**Registry.** Reads ``audittrace.tools.mcp_write_registry.
MCP_WRITE_TOOL_REGISTRY`` — NOT ``audittrace.tools.MEMORY_TOOL_REGISTRY``.
This module never imports ``audittrace.tools.tools_visible_to`` or
``audittrace.tools.get_tool_by_name`` and never touches
``MEMORY_TOOL_REGISTRY``, so ``/v1/chat/completions`` and
``services.mcp_bridge`` (Phase 1) are structurally unaffected by this
module's existence — see ``tools/mcp_write_handlers.py``'s and
``tools/mcp_write_registry.py``'s module docstrings for the full
rationale.

**Per-tool scope, not per-server/tier (the Track B review's design
input, applied here).** Track B's broker gates on one
``required_scope`` per DOWNSTREAM SERVER — every tool a given server
exposes shares that one grant. A write/curation tool is a mutation on a
specific curated collection; ``list_write_tools``/``call_write_tool``
check each tool's OWN ``MemoryTool.required_scope`` individually (the
same mechanism ``mcp_bridge.call_read_tool`` already uses for read
tools) — holding ``memory:decisions:write`` never authorizes
``write_skill``, and vice versa.

**No ``is_admin`` bypass** (deliberate deviation from
``mcp_bridge.list_read_tools``/``call_read_tool``'s admin-bypass
convention — see ``tools/mcp_write_handlers.py``'s module docstring for
why: it keeps the sentinel/dev-mode bypass identity, which is
``is_admin=True`` with neither write scope literally granted, from
silently gaining two mutation tools).

**No result cache** (deliberate deviation from
``audittrace.tools.invoke_tool``, which ``call_read_tool`` uses). A
cache-hit on a WRITE would replay a stale "success" without
re-executing the mutation and, worse, without writing a NEW audit row —
breaking the tamper-evident "record every call" invariant. Every
``call_write_tool`` invocation dispatches the handler directly and
produces exactly one ``PendingToolCall``.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

import mcp.types as mcp_types

# Side-effect import (not just the registry): running this module is
# what runs the ``write_decision``/``write_skill``
# ``@register_mcp_write_tool`` decorators the first time anything
# imports this bridge — mirrors ``routes/chat.py``'s equivalent
# side-effect import of ``audittrace.tools.memory_handlers`` for Phase 1.
import audittrace.tools.mcp_write_handlers  # noqa: F401
from audittrace.identity import UserContext
from audittrace.routes._memory_tool_loop import PendingToolCall
from audittrace.services.mcp_bridge import McpCallOutcome
from audittrace.tools import MemoryTool
from audittrace.tools.mcp_write_registry import MCP_WRITE_TOOL_REGISTRY

logger = logging.getLogger(__name__)

# Caller-supplied argument keys stripped before dispatch — same guard as
# ``mcp_bridge._strip_identity_args``
# (``feedback_never_trust_caller_metadata_for_security_fields``).
_IDENTITY_ARG_KEYS = frozenset({"user_id", "sub", "userId"})


def list_write_tools(user_context: UserContext) -> list[MemoryTool]:
    """Return the write/curation tools visible to ``user_context``.

    Falsifiable acceptance: a caller WITHOUT a tool's literal
    ``required_scope`` never sees it here, regardless of
    ``is_admin`` — see module docstring."""
    visible: list[MemoryTool] = []
    for tool in MCP_WRITE_TOOL_REGISTRY.values():
        if not tool.enabled:
            continue
        if tool.required_scope not in user_context.scopes:
            continue
        visible.append(tool)
    return visible


def get_write_tool_by_name(name: str) -> MemoryTool | None:
    """Resolve an MCP tool name to a registered write tool, or ``None``
    if the name isn't a registered write tool (or is disabled).

    Deliberately does NOT check scope — this is routing (is this NAME a
    write tool at all), used by ``routes/mcp.py`` to decide which
    dispatcher a ``tools/call`` reaches. The scope gate is enforced in
    ``call_write_tool`` regardless of what ``tools/list`` advertised —
    same defense-in-depth shape Phase 1 uses."""
    tool = MCP_WRITE_TOOL_REGISTRY.get(name)
    if tool is None or not tool.enabled:
        return None
    return tool


def _text_result(
    payload: dict[str, Any], *, is_error: bool
) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=json.dumps(payload))],
        structuredContent=None if is_error else payload,
        isError=is_error,
    )


def _denied(
    *,
    tool_name: str,
    user_context: UserContext,
    arguments: dict[str, Any],
    started: datetime,
    perf_start: float,
    granted_scope: str,
    error: str,
    failure_class: str,
    log_reason: str,
) -> McpCallOutcome:
    logger.info(
        "mcp write tools/call denied tool=%s user=%s reason=%s",
        tool_name,
        user_context.user_id,
        log_reason,
    )
    return McpCallOutcome(
        result=_text_result({"error": error}, is_error=True),
        pending=PendingToolCall(
            tool_name=tool_name,
            user_id=user_context.user_id,
            agent_type=user_context.agent_type,
            args=json.dumps(arguments),
            result_summary=None,
            error=error,
            started_at=started,
            duration_ms=int((time.perf_counter() - perf_start) * 1000),
            granted_scope=granted_scope,
        ),
        status="failed",
        failure_class=failure_class,
        error_detail=error,
    )


async def call_write_tool(
    *,
    user_context: UserContext,
    tool_name: str,
    arguments: dict[str, Any],
) -> McpCallOutcome:
    """Authorize → execute → record → return, for MCP write/curation
    tools (ADR-063 Phase 2 Track A).

    ``user_context`` MUST already be token-derived (bound by
    ``require_user`` before this is ever called) — never resolves
    identity itself, and never trusts anything inside ``arguments`` as
    identity (``_IDENTITY_ARG_KEYS`` stripped below)."""
    started = datetime.now()
    perf_start = time.perf_counter()
    arguments = {k: v for k, v in arguments.items() if k not in _IDENTITY_ARG_KEYS}

    tool = get_write_tool_by_name(tool_name)
    if tool is None:
        return _denied(
            tool_name=tool_name,
            user_context=user_context,
            arguments=arguments,
            started=started,
            perf_start=perf_start,
            granted_scope="",
            error=f"unknown MCP write tool: {tool_name}",
            failure_class="tool_not_found",
            log_reason="unknown_tool",
        )

    if tool.required_scope not in user_context.scopes:
        # No mutation, no data — the handler is never called on this
        # branch. Falsifiable acceptance: neuter this check (e.g. always
        # ``True``) → the denied-write test goes RED because the mock
        # semantic-service upsert spy would then have been called.
        return _denied(
            tool_name=tool_name,
            user_context=user_context,
            arguments=arguments,
            started=started,
            perf_start=perf_start,
            granted_scope=tool.required_scope,
            error=(
                f"scope denied: {tool.required_scope} not in caller scopes "
                f"for tool {tool_name}"
            ),
            failure_class="scope_denied",
            log_reason="scope_denied",
        )

    try:
        result = await tool.handler(user_context, arguments)
    except Exception as exc:
        logger.exception("mcp write tools/call handler %r raised", tool_name)
        error_text = exc.__class__.__name__
        return McpCallOutcome(
            result=_text_result({"error": error_text}, is_error=True),
            pending=PendingToolCall(
                tool_name=tool_name,
                user_id=user_context.user_id,
                agent_type=user_context.agent_type,
                args=json.dumps(arguments),
                result_summary=None,
                error=error_text,
                started_at=started,
                duration_ms=int((time.perf_counter() - perf_start) * 1000),
                granted_scope=tool.required_scope,
            ),
            status="failed",
            failure_class="tool_error",
            error_detail=error_text,
        )

    duration_ms = int((time.perf_counter() - perf_start) * 1000)

    if "error" in result:
        error_text = str(result.get("error"))
        logger.info(
            "mcp write tools/call error tool=%s user=%s error=%s",
            tool_name,
            user_context.user_id,
            error_text,
        )
        return McpCallOutcome(
            result=_text_result(result, is_error=True),
            pending=PendingToolCall(
                tool_name=tool_name,
                user_id=user_context.user_id,
                agent_type=user_context.agent_type,
                args=json.dumps(arguments),
                result_summary=None,
                error=error_text,
                started_at=started,
                duration_ms=duration_ms,
                granted_scope=tool.required_scope,
            ),
            status="failed",
            failure_class="tool_error",
            error_detail=error_text,
        )

    summary = json.dumps(result)[:1000]
    logger.info(
        "mcp write tools/call ok tool=%s user=%s duration_ms=%d",
        tool_name,
        user_context.user_id,
        duration_ms,
    )
    return McpCallOutcome(
        result=_text_result(result, is_error=False),
        pending=PendingToolCall(
            tool_name=tool_name,
            user_id=user_context.user_id,
            agent_type=user_context.agent_type,
            args=json.dumps(arguments),
            result_summary=summary,
            error=None,
            started_at=started,
            duration_ms=duration_ms,
            granted_scope=tool.required_scope,
        ),
        status="success",
        failure_class=None,
        error_detail=None,
    )
