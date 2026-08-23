"""Bridge: MCP ``tools/call`` → the existing memory-tool registry + audit
path (ADR-063 Phase 1).

This module is intentionally thin. It does not reimplement
authorization, execution, or auditing — it composes three seams that
already exist:

1. ``audittrace.tools`` — ``MEMORY_TOOL_REGISTRY`` and the cache-aware
   ``invoke_tool`` dispatcher (ADR-025).
2. ``audittrace.routes._memory_tool_loop.PendingToolCall`` — the SAME
   audit-record shape the chat tool-call loop accumulates.
3. ``audittrace.routes.chat._persist_interaction`` /
   ``_flush_pending_tool_calls`` — the SAME tamper-evident write path
   (ADR-037/058). MCP is a new *transport* over this record, not a
   second recorder — the route module (``routes/mcp.py``) calls those
   two functions directly with the ``PendingToolCall`` this module
   returns.

**Phase 1 boundary (ADR-063).** Only READ/recall tools are ever listed
or callable through MCP. ``_is_read_scope`` is the single predicate
that enforces this. Unlike ``audittrace.tools.tools_visible_to``, the
boundary here applies EVEN TO ADMINS — Phase 1 must not have an
admin escape hatch, or a write/curation tool registered in a later
phase would leak into an admin's MCP manifest and dispatcher the day
it lands, without anyone touching this file again.

**Identity (ADR-063 grounding).** Every function here takes an already
-resolved ``UserContext`` (token-derived, bound by
``audittrace.auth.require_user`` before this module is ever reached).
Caller-supplied identity fields inside the JSON-RPC ``arguments`` dict
are stripped before dispatch — the token's ``sub`` always wins
(``feedback_never_trust_caller_metadata_for_security_fields``).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import mcp.types as mcp_types

from audittrace.identity import UserContext
from audittrace.routes._memory_tool_loop import PendingToolCall
from audittrace.tools import (
    MEMORY_TOOL_REGISTRY,
    MemoryTool,
    get_tool_by_name,
    invoke_tool,
)

logger = logging.getLogger(__name__)

# A tool is MCP-Phase-1-eligible only when its required_scope ends in one
# of these suffixes. Positive allowlist (not "doesn't end in :write") so
# an unanticipated future scope shape fails closed rather than leaking by
# omission.
_READ_SCOPE_SUFFIXES = (":read", ":read-own")

# Caller-supplied argument keys that could be mistaken for identity by a
# careless future handler. Stripped unconditionally before dispatch —
# defense in depth on top of the fact that no current handler reads them.
_IDENTITY_ARG_KEYS = frozenset({"user_id", "sub", "userId"})


def _is_read_scope(scope: str) -> bool:
    """Phase 1 boundary predicate — the single place this is decided."""
    return scope.endswith(_READ_SCOPE_SUFFIXES)


def list_read_tools(user_context: UserContext) -> list[MemoryTool]:
    """Return the enabled, read-scoped tools visible to ``user_context``.

    Scope-visibility mirrors ``audittrace.tools.tools_visible_to``
    (admins bypass the ``required_scope`` check), but the read-only
    ``_is_read_scope`` gate is applied first and is NOT bypassed by
    ``is_admin`` — that is the Phase 1 boundary the falsifiable
    acceptance suite pins ("leak a write tool into the manifest" must
    stay RED regardless of caller privilege).
    """
    visible: list[MemoryTool] = []
    for tool in MEMORY_TOOL_REGISTRY.values():
        if not tool.enabled:
            continue
        if not _is_read_scope(tool.required_scope):
            continue
        if not user_context.is_admin and tool.required_scope not in user_context.scopes:
            continue
        visible.append(tool)
    return visible


def to_mcp_tool(tool: MemoryTool) -> mcp_types.Tool:
    """Render a ``MemoryTool`` as an ``mcp.types.Tool`` manifest entry."""
    return mcp_types.Tool(
        name=tool.name,
        description=tool.description,
        inputSchema=tool.parameters_schema,
    )


def _strip_identity_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Drop any caller-supplied identity-shaped keys before dispatch.

    The token-derived ``UserContext`` is the only identity a handler
    ever receives (as its own first argument) — no current handler
    reads ``args["user_id"]``, but this makes the guarantee explicit
    and enforced at the MCP boundary rather than merely incidental.
    """
    return {k: v for k, v in arguments.items() if k not in _IDENTITY_ARG_KEYS}


def _text_result(
    payload: dict[str, Any], *, is_error: bool
) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=json.dumps(payload))],
        structuredContent=None if is_error else payload,
        isError=is_error,
    )


@dataclass
class McpCallOutcome:
    """Result of one ``tools/call`` dispatch, paired with its audit record.

    Exactly one ``McpCallOutcome`` — and therefore exactly one
    ``PendingToolCall`` — is produced per ``call_read_tool`` invocation,
    regardless of which branch (unknown tool / write-scope boundary /
    missing scope / handler error / handler success) it takes. The
    route layer writes exactly one audit row per call from this one
    record — the same "one call, one row" invariant the chat tool loop
    keeps.
    """

    result: mcp_types.CallToolResult
    pending: PendingToolCall
    status: str  # "success" | "failed" — mirrors InteractionRecord.status
    failure_class: str | None
    error_detail: str | None


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
    """Shared denial-path builder for the unknown-tool / write-boundary /
    scope-denied branches — same shape as ``_execute_memory_tool``'s
    denial branches in the chat tool loop, kept as one helper here so
    the three call sites can't drift from each other."""
    logger.info(
        "mcp tools/call denied tool=%s user=%s reason=%s",
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


async def call_read_tool(
    *,
    user_context: UserContext,
    session_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> McpCallOutcome:
    """Authorize → execute → record → return (ADR-063 Phase 1 §Change.3).

    ``user_context`` MUST already be token-derived (bound by
    ``require_user`` before this is called) — this function never
    resolves identity itself, and never trusts anything inside
    ``arguments`` as identity (``_strip_identity_args``).
    """
    started = datetime.now()
    perf_start = time.perf_counter()
    arguments = _strip_identity_args(arguments)

    tool = get_tool_by_name(tool_name)
    if tool is None:
        return _denied(
            tool_name=tool_name,
            user_context=user_context,
            arguments=arguments,
            started=started,
            perf_start=perf_start,
            granted_scope="",
            error=f"unknown memory tool: {tool_name}",
            failure_class="tool_not_found",
            log_reason="unknown_tool",
        )

    if not _is_read_scope(tool.required_scope):
        # Defense in depth — list_read_tools already excludes this tool
        # from the advertised manifest, but an MCP client can call
        # tools/call with any name it likes regardless of what tools/list
        # returned. The Phase 1 write-tool boundary is enforced at BOTH
        # the manifest and the dispatch edge, with no admin bypass.
        return _denied(
            tool_name=tool_name,
            user_context=user_context,
            arguments=arguments,
            started=started,
            perf_start=perf_start,
            granted_scope=tool.required_scope,
            error=(
                f"tool {tool_name!r} is outside the MCP Phase 1 read/recall "
                "surface (ADR-063)"
            ),
            failure_class="phase1_write_boundary",
            log_reason="write_scope_boundary",
        )

    if not user_context.is_admin and tool.required_scope not in user_context.scopes:
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

    # Dispatch through the SAME cache-aware helper the chat tool loop
    # uses. was_cache_hit only affects telemetry here — Phase 1 still
    # writes exactly one audit row per MCP call either way (unlike the
    # chat loop, an MCP tools/call has no "parent interaction" that a
    # cache-hit row would be redundant against).
    result, _was_cache_hit = await invoke_tool(
        user_context, tool, arguments, session_id
    )
    duration_ms = int((time.perf_counter() - perf_start) * 1000)

    if "error" in result:
        error_text = str(result.get("error"))
        logger.info(
            "mcp tools/call error tool=%s user=%s error=%s",
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
        "mcp tools/call ok tool=%s user=%s duration_ms=%d",
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
